import asyncio
import logging
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Request, Response, WebSocket, WebSocketDisconnect, status
from fastapi.responses import StreamingResponse
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import websockets

from app.auth.dependencies import get_current_user_optional, get_token_from_request
from app.database import get_db
from app.models.user import User, UserRole
from app.models.workspace import Workspace, WorkspaceStatus

logger = logging.getLogger("devcloud.proxy")
proxy_router = APIRouter(prefix="/proxy", tags=["Proxy"])


async def get_authorized_workspace(
    workspace_id: str,
    db: AsyncSession,
    current_user: User | None,
) -> Workspace:
    """Validate that the workspace exists and user has permission to access it."""
    stmt = select(Workspace).where(Workspace.id == workspace_id)
    res = await db.execute(stmt)
    workspace = res.scalar_one_or_none()

    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found.")

    if not current_user:
        raise HTTPException(status_code=401, detail="Authentication required to access workspace.")

    if workspace.user_id != current_user.id and current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Permission denied for this workspace.")

    if workspace.status != WorkspaceStatus.RUNNING:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Workspace is not running (current status: {workspace.status}). Please start it first.",
        )

    return workspace


@proxy_router.api_route(
    "/{workspace_id}/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
)
async def proxy_http_request(
    workspace_id: str,
    path: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
):
    """Proxy HTTP requests to the target container's local port."""
    workspace = await get_authorized_workspace(workspace_id, db, current_user)
    target_url = f"http://127.0.0.1:{workspace.host_port}/{path}"
    
    if request.url.query:
        target_url = f"{target_url}?{request.url.query}"

    # Filter headers to pass through
    excluded_headers = {"host", "content-length"}
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in excluded_headers
    }
    # Set host header to localhost target
    headers["Host"] = f"127.0.0.1:{workspace.host_port}"

    body = await request.body()

    try:
        client = httpx.AsyncClient(timeout=60.0)
        req = client.build_request(
            method=request.method,
            url=target_url,
            headers=headers,
            content=body,
        )
        resp = await client.send(req, stream=True)

        # Build response streaming
        async def response_stream():
            try:
                async for chunk in resp.aiter_raw():
                    yield chunk
            finally:
                await resp.aclose()
                await client.aclose()

        response_headers = dict(resp.headers)
        # Remove hop-by-hop headers
        for h in ["content-encoding", "transfer-encoding", "content-length"]:
            response_headers.pop(h, None)

        return StreamingResponse(
            response_stream(),
            status_code=resp.status_code,
            headers=response_headers,
            media_type=resp.headers.get("content-type"),
        )
    except httpx.ConnectError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not connect to workspace container. It may still be starting up.",
        )
    except Exception as e:
        logger.error(f"Proxy error for workspace {workspace_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Proxy error: {str(e)}")


@proxy_router.websocket("/{workspace_id}/{path:path}")
async def proxy_websocket(
    websocket: WebSocket,
    workspace_id: str,
    path: str,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Proxy WebSocket traffic (critical for code-server terminals and IDE sync)."""
    # Accept client websocket
    await websocket.accept()

    # Extract auth token from query params or cookies
    token = websocket.query_params.get("token") or websocket.cookies.get("devcloud_session")
    from app.auth.internal import decode_access_token
    payload = decode_access_token(token) if token else None
    user_id = payload.get("user_id") if payload else None

    current_user = None
    if user_id:
        stmt = select(User).where(User.id == int(user_id))
        res = await db.execute(stmt)
        current_user = res.scalar_one_or_none()

    try:
        workspace = await get_authorized_workspace(workspace_id, db, current_user)
    except HTTPException as e:
        await websocket.close(code=4003, reason=str(e.detail))
        return

    target_ws_url = f"ws://127.0.0.1:{workspace.host_port}/{path}"
    if websocket.scope.get("query_string"):
        target_ws_url += f"?{websocket.scope['query_string'].decode()}"

    try:
        async with websockets.connect(target_ws_url) as target_ws:
            async def forward_to_target():
                try:
                    while True:
                        msg = await websocket.receive()
                        if "text" in msg:
                            await target_ws.send(msg["text"])
                        elif "bytes" in msg:
                            await target_ws.send(msg["bytes"])
                except (WebSocketDisconnect, asyncio.CancelledError):
                    pass

            async def forward_to_client():
                try:
                    while True:
                        msg = await target_ws.recv()
                        if isinstance(msg, str):
                            await websocket.send_text(msg)
                        else:
                            await websocket.send_bytes(msg)
                except (websockets.ConnectionClosed, asyncio.CancelledError):
                    pass

            await asyncio.gather(
                forward_to_target(),
                forward_to_client(),
                return_exceptions=True,
            )
    except Exception as exc:
        logger.warning(f"WebSocket proxy closed: {exc}")
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
