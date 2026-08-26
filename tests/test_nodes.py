import hashlib

import pytest
from fastapi import WebSocketDisconnect
from httpx import AsyncClient
from sqlalchemy import update

from app.models.node import Node, NodeStatus
from app.models.user import User, UserRole
from app.models.workspace import Workspace, WorkspaceStatus
from app.orchestrator.flavors import get_flavor
from app.orchestrator.scheduler import NoSchedulableNode, select_worker_node
from app.routes.agent_routes import connect_agent
from tests.conftest import TestingSessionLocal


async def _admin_headers(client: AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/auth/register",
        json={"username": "node_admin", "email": "node-admin@test.com", "password": "Password123!"},
    )
    token = response.json()["access_token"]
    user_id = response.json()["user"]["id"]
    async with TestingSessionLocal() as session:
        await session.execute(update(User).where(User.id == user_id).values(role=UserRole.ADMIN))
        await session.commit()
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_admin_can_enroll_and_drain_worker_without_revealing_token_later(client: AsyncClient):
    headers = await _admin_headers(client)
    created = await client.post(
        "/api/admin/nodes",
        headers=headers,
        json={"name": "cpu-worker-01", "schedulable": True, "labels": {"zone": "dc1"}},
    )
    assert created.status_code == 201
    body = created.json()
    assert body["enrollment_token"]
    assert body["status"] == "pending"

    listed = await client.get("/api/admin/nodes", headers=headers)
    assert listed.status_code == 200
    assert listed.json()[0]["name"] == "cpu-worker-01"
    assert "enrollment_token" not in listed.json()[0]

    drained = await client.patch(
        f"/api/admin/nodes/{body['id']}",
        headers=headers,
        json={"schedulable": False},
    )
    assert drained.status_code == 200
    assert drained.json()["schedulable"] is False


@pytest.mark.asyncio
async def test_scheduler_uses_online_cpu_worker_and_never_falls_back_when_workers_exist(db_session, monkeypatch):
    worker = Node(
        name="cpu-worker-02",
        status=NodeStatus.ONLINE,
        enabled=True,
        schedulable=True,
        cpu_total=8,
        memory_total_mb=16384,
        disk_total_mb=100000,
        agent_token_hash="0" * 64,
    )
    db_session.add(worker)
    await db_session.commit()

    monkeypatch.setattr(
        "app.orchestrator.scheduler.agent_manager.is_connected",
        lambda node_id: node_id == worker.id,
    )

    selected = await select_worker_node(db_session, get_flavor("t1.small"))
    assert selected.id == worker.id

    worker.status = NodeStatus.OFFLINE
    db_session.add(worker)
    await db_session.commit()
    with pytest.raises(NoSchedulableNode):
        await select_worker_node(db_session, get_flavor("t1.small"))


@pytest.mark.asyncio
async def test_agent_heartbeat_preserves_admin_drain_change(db_session):
    token = "node-secret"
    worker = Node(
        name="cpu-worker-drain",
        status=NodeStatus.PENDING,
        enabled=True,
        schedulable=True,
        agent_token_hash=hashlib.sha256(token.encode()).hexdigest(),
    )
    db_session.add(worker)
    await db_session.commit()
    await db_session.refresh(worker)

    class FakeWebSocket:
        headers = {"authorization": f"Bearer {token}"}

        def __init__(self):
            self.receive_count = 0
            self.status_during_connection = None

        async def accept(self):
            return None

        async def close(self, **_kwargs):
            return None

        async def receive_json(self):
            self.receive_count += 1
            if self.receive_count == 1:
                async with TestingSessionLocal() as admin_session:
                    await admin_session.execute(
                        update(Node)
                        .where(Node.id == worker.id)
                        .values(schedulable=False, status=NodeStatus.DRAINING)
                    )
                    await admin_session.commit()
                return {
                    "type": "heartbeat",
                    "payload": {
                        "hostname": "worker-drain",
                        "cpu_total": 8,
                        "memory_total_mb": 16384,
                        "disk_total_mb": 100000,
                        "capabilities": {"runtime": "podman"},
                        "agent_version": "test",
                    },
                }
            async with TestingSessionLocal() as observer:
                fresh = await observer.get(Node, worker.id)
                self.status_during_connection = fresh.status
            raise WebSocketDisconnect()

    websocket = FakeWebSocket()
    await connect_agent(websocket, worker.id, db_session)

    assert websocket.status_during_connection == NodeStatus.DRAINING


@pytest.mark.asyncio
async def test_admin_can_delete_worker(client: AsyncClient, db_session):
    headers = await _admin_headers(client)
    created = await client.post(
        "/api/admin/nodes",
        headers=headers,
        json={"name": "cpu-worker-delete-me", "schedulable": True, "labels": {}},
    )
    assert created.status_code == 201
    node_id = created.json()["id"]

    delete_resp = await client.delete(f"/api/admin/nodes/{node_id}", headers=headers)
    assert delete_resp.status_code == 200
    assert "başarıyla silindi" in delete_resp.json()["message"]

    get_resp = await client.get("/api/admin/nodes", headers=headers)
    assert get_resp.status_code == 200
    assert not any(n["id"] == node_id for n in get_resp.json())


@pytest.mark.asyncio
async def test_admin_delete_worker_not_found(client: AsyncClient):
    headers = await _admin_headers(client)
    delete_resp = await client.delete("/api/admin/nodes/non-existent-id", headers=headers)
    assert delete_resp.status_code == 404


@pytest.mark.asyncio
async def test_admin_cannot_delete_worker_with_active_workspaces(client: AsyncClient, db_session):
    headers = await _admin_headers(client)
    created = await client.post(
        "/api/admin/nodes",
        headers=headers,
        json={"name": "cpu-worker-busy", "schedulable": True, "labels": {}},
    )
    node_id = created.json()["id"]

    # Create active workspace assigned to this node
    ws = Workspace(
        name="test-ws",
        user_id=1,
        node_id=node_id,
        template_id="vscode-python",
        flavor_id="t1.small",
        container_name="cnt-test-ws",
        host_port=20001,
        storage_path="/tmp/test-ws",
        status=WorkspaceStatus.RUNNING,
    )
    db_session.add(ws)
    await db_session.commit()

    delete_resp = await client.delete(f"/api/admin/nodes/{node_id}", headers=headers)
    assert delete_resp.status_code == 400
    assert "aktif çalışma alanı" in delete_resp.json()["detail"]


@pytest.mark.asyncio
async def test_admin_can_delete_worker_with_stopped_workspaces_and_nulls_node_id(client: AsyncClient, db_session):
    headers = await _admin_headers(client)
    created = await client.post(
        "/api/admin/nodes",
        headers=headers,
        json={"name": "cpu-worker-idle", "schedulable": True, "labels": {}},
    )
    node_id = created.json()["id"]

    # Create stopped workspace assigned to this node
    ws = Workspace(
        name="test-stopped-ws",
        user_id=1,
        node_id=node_id,
        template_id="vscode-python",
        flavor_id="t1.small",
        container_name="cnt-test-stopped-ws",
        host_port=20002,
        storage_path="/tmp/test-stopped-ws",
        status=WorkspaceStatus.STOPPED,
    )
    db_session.add(ws)
    await db_session.commit()

    delete_resp = await client.delete(f"/api/admin/nodes/{node_id}", headers=headers)
    assert delete_resp.status_code == 200

    await db_session.refresh(ws)
    assert ws.node_id is None

