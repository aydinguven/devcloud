import hashlib
import json
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.manager import agent_manager
from app.database import get_db
from app.models.node import Node, NodeStatus
from app.schemas.node import NodeHeartbeat

agent_router = APIRouter(prefix="/api/agent", tags=["Worker Agent"])


def _bearer_token(websocket: WebSocket) -> str:
    value = websocket.headers.get("authorization", "")
    scheme, _, token = value.partition(" ")
    return token if scheme.lower() == "bearer" else ""


@agent_router.websocket("/connect/{node_id}")
async def connect_agent(
    websocket: WebSocket,
    node_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Accept the worker-initiated, persistent control/data tunnel."""
    node = await db.get(Node, node_id)
    token_hash = hashlib.sha256(_bearer_token(websocket).encode("utf-8")).hexdigest()
    if not node or not node.enabled or not secrets.compare_digest(node.agent_token_hash, token_hash):
        await websocket.close(code=4403, reason="Geçersiz worker kimliği")
        return

    await websocket.accept()
    connection = await agent_manager.register(node.id, websocket)
    node.status = NodeStatus.ONLINE
    node.last_seen_at = datetime.now(timezone.utc)
    db.add(node)
    await db.commit()
    try:
        while True:
            message = await websocket.receive_json()
            if message.get("type") == "heartbeat":
                heartbeat = NodeHeartbeat.model_validate(message.get("payload") or {})
                # Admin changes happen in another request/session. Refresh the
                # policy fields before applying heartbeat data so a stale,
                # long-lived agent session cannot undo drain/disable decisions.
                await db.refresh(node, attribute_names=["enabled", "schedulable"])
                if not node.enabled:
                    await websocket.close(
                        code=4403,
                        reason="Worker yönetici tarafından devre dışı bırakıldı",
                    )
                    break
                node.hostname = heartbeat.hostname
                node.cpu_total = heartbeat.cpu_total
                node.memory_total_mb = heartbeat.memory_total_mb
                node.disk_total_mb = heartbeat.disk_total_mb
                node.capabilities_json = json.dumps(heartbeat.capabilities, ensure_ascii=False)
                node.agent_version = heartbeat.agent_version
                node.status = NodeStatus.DRAINING if not node.schedulable else NodeStatus.ONLINE
                node.last_seen_at = datetime.now(timezone.utc)
                db.add(node)
                await db.commit()
            else:
                await connection.handle_message(message)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        await agent_manager.unregister(node.id, connection)
        fresh_node = await db.get(Node, node.id)
        if fresh_node:
            fresh_node.status = NodeStatus.OFFLINE
            db.add(fresh_node)
            await db.commit()
