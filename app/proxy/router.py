import asyncio
import logging
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Request, Response, WebSocket, WebSocketDisconnect, status
from fastapi.responses import HTMLResponse, StreamingResponse
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


def get_upstream_path(workspace_id: str, template_id: str, path: str) -> str:
    """Keep Jupyter's configured proxy prefix while other IDEs use root paths."""
    normalized_path = path.lstrip("/")
    if "jupyter" in template_id:
        return f"/proxy/{workspace_id}/{normalized_path}"
    return f"/{normalized_path}"


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
    upstream_path = get_upstream_path(workspace.id, workspace.template_id, path)
    target_url = f"http://127.0.0.1:{workspace.host_port}{upstream_path}"
    
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
    if "jupyter" in workspace.template_id:
        # Authenticate only the trusted loopback hop; never expose this token.
        headers["Authorization"] = f"token {workspace.workspace_token}"

    body = await request.body()
    client = httpx.AsyncClient(timeout=30.0)

    # Retry loop to gracefully handle initial container service boot time (2-5s)
    max_retries = 6
    resp = None

    for attempt in range(max_retries):
        try:
            req = client.build_request(
                method=request.method,
                url=target_url,
                headers=headers,
                content=body,
            )
            resp = await client.send(req, stream=True)
            break
        except httpx.ConnectError:
            if attempt < max_retries - 1:
                await asyncio.sleep(1.0)
            else:
                await client.aclose()
                # Return auto-refreshing HTML waiting screen for GET browser requests
                if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
                    html_content = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta http-equiv="refresh" content="2">
    <title>Starting {workspace.name} - DevCloud</title>
    <style>
        body {{ background: #0f172a; color: #f8fafc; font-family: -apple-system, BlinkMacSystemFont, sans-serif; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }}
        .card {{ background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 2.5rem; text-align: center; max-width: 460px; box-shadow: 0 4px 6px rgba(0,0,0,0.2); }}
        .spinner {{ width: 44px; height: 44px; border: 4px solid #334155; border-top-color: #38bdf8; border-radius: 50%; animation: spin 1s linear infinite; margin: 0 auto 1.5rem auto; }}
        @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
        h1 {{ font-size: 1.35rem; margin-bottom: 0.5rem; }}
        p {{ color: #94a3b8; font-size: 0.9rem; margin-bottom: 1.5rem; }}
        code {{ background: #0f172a; padding: 0.2rem 0.5rem; border-radius: 4px; color: #38bdf8; font-size: 0.8rem; }}
    </style>
</head>
<body>
    <div class="card">
        <div class="spinner"></div>
        <h1>Initializing {workspace.name}...</h1>
        <p>The container IDE service is starting up on port <code>{workspace.host_port}</code>. This page will connect automatically in a few seconds.</p>
    </div>
</body>
</html>"""
                    return HTMLResponse(content=html_content, status_code=200)

                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="Could not connect to workspace container. It may still be starting up.",
                )
        except Exception as e:
            await client.aclose()
            logger.error(f"Proxy error for workspace {workspace_id}: {e}")
            raise HTTPException(status_code=500, detail=f"Proxy error: {str(e)}")

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
    for h in ["transfer-encoding", "content-length"]:
        response_headers.pop(h, None)

    return StreamingResponse(
        response_stream(),
        status_code=resp.status_code,
        headers=response_headers,
        media_type=resp.headers.get("content-type"),
    )


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

    upstream_path = get_upstream_path(workspace.id, workspace.template_id, path)
    target_ws_url = f"ws://127.0.0.1:{workspace.host_port}{upstream_path}"
    if websocket.scope.get("query_string"):
        target_ws_url += f"?{websocket.scope['query_string'].decode()}"

    additional_headers = None
    if "jupyter" in workspace.template_id:
        additional_headers = {"Authorization": f"token {workspace.workspace_token}"}

    try:
        async with websockets.connect(
            target_ws_url, additional_headers=additional_headers
        ) as target_ws:
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
