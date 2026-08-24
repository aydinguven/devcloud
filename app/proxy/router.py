import asyncio
import logging
from html import escape
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
from app.orchestrator.podman_service import podman_service

logger = logging.getLogger("devcloud.proxy")
proxy_router = APIRouter(prefix="/proxy", tags=["Proxy"])


async def get_authorized_workspace(
    workspace_id: str,
    db: AsyncSession,
    current_user: User | None,
    require_running: bool = True,
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

    if require_running and workspace.status != WorkspaceStatus.RUNNING:
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


STARTING_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Starting __WORKSPACE_NAME__ - DevCloud</title>
    <style>
        :root { color-scheme: dark; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
        * { box-sizing: border-box; }
        body { min-height: 100vh; margin: 0; padding: 28px; background: radial-gradient(circle at top, #172554 0, #0f172a 42%, #020617 100%); color: #e2e8f0; }
        .shell { width: min(940px, 100%); margin: 0 auto; }
        .header { display: flex; align-items: center; gap: 16px; margin-bottom: 20px; }
        .spinner { flex: 0 0 auto; width: 42px; height: 42px; border: 4px solid #334155; border-top-color: #38bdf8; border-radius: 50%; animation: spin 1s linear infinite; }
        @keyframes spin { to { transform: rotate(360deg); } }
        h1 { margin: 0 0 4px; font-size: clamp(1.3rem, 3vw, 1.8rem); }
        .subtitle { margin: 0; color: #94a3b8; }
        .panel { background: rgba(15, 23, 42, .92); border: 1px solid #334155; border-radius: 14px; box-shadow: 0 20px 50px rgba(0, 0, 0, .28); overflow: hidden; }
        .status-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 1px; background: #334155; border-bottom: 1px solid #334155; }
        .metric { min-height: 88px; padding: 16px; background: #111c31; }
        .metric-label { display: block; margin-bottom: 9px; color: #64748b; font-size: .72rem; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }
        .metric-value { color: #f8fafc; font-size: .94rem; font-weight: 650; overflow-wrap: anywhere; }
        .pill { display: inline-flex; align-items: center; gap: 7px; }
        .pill::before { content: ""; width: 8px; height: 8px; border-radius: 50%; background: #f59e0b; box-shadow: 0 0 12px currentColor; }
        .pill.ready::before { background: #22c55e; }
        .pill.error::before { background: #ef4444; }
        .log-head { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 14px 18px; border-bottom: 1px solid #1e293b; }
        .log-head strong { font-size: .88rem; }
        .log-head span { color: #64748b; font-size: .76rem; }
        pre { min-height: 280px; max-height: 48vh; margin: 0; padding: 18px; overflow: auto; background: #020617; color: #cbd5e1; font: 12px/1.55 "SFMono-Regular", Consolas, "Liberation Mono", monospace; white-space: pre-wrap; overflow-wrap: anywhere; }
        .footer { display: flex; align-items: center; justify-content: space-between; gap: 14px; padding: 16px 18px; }
        .message { margin: 0; color: #94a3b8; font-size: .84rem; }
        .actions { display: flex; gap: 9px; flex-wrap: wrap; }
        button, a { border: 1px solid #475569; border-radius: 8px; padding: 8px 12px; background: #1e293b; color: #e2e8f0; font: inherit; font-size: .82rem; text-decoration: none; cursor: pointer; }
        button:hover, a:hover { border-color: #38bdf8; color: #fff; }
        code { color: #7dd3fc; }
        @media (max-width: 620px) { body { padding: 18px 12px; } .footer { align-items: flex-start; flex-direction: column; } pre { min-height: 230px; } }
    </style>
</head>
<body>
<main class="shell" id="startup" data-status-url="__STATUS_URL__">
    <div class="header">
        <div class="spinner" id="spinner"></div>
        <div>
            <h1 id="headline">Initializing __WORKSPACE_NAME__...</h1>
            <p class="subtitle">Waiting for the IDE service on host port <code>__HOST_PORT__</code>.</p>
        </div>
    </div>
    <section class="panel" aria-live="polite">
        <div class="status-grid">
            <div class="metric"><span class="metric-label">Workspace</span><span class="metric-value pill" id="workspace-status">running</span></div>
            <div class="metric"><span class="metric-label">Container</span><span class="metric-value pill" id="container-status">checking</span></div>
            <div class="metric"><span class="metric-label">IDE port</span><span class="metric-value pill" id="port-status">checking :__HOST_PORT__</span></div>
            <div class="metric"><span class="metric-label">Elapsed</span><span class="metric-value" id="elapsed">0s</span></div>
            <div class="metric"><span class="metric-label">Checks</span><span class="metric-value" id="attempts">0</span></div>
        </div>
        <div class="log-head"><strong>Recent container output</strong><span>__TEMPLATE_ID__ · refreshes every 2 seconds</span></div>
        <pre id="logs">Requesting container logs...</pre>
        <div class="footer">
            <p class="message" id="message">The page will open automatically when the IDE port accepts connections.</p>
            <div class="actions">
                <button type="button" id="retry">Retry now</button>
                <a href="__DETAIL_URL__">Workspace details</a>
            </div>
        </div>
    </section>
</main>
<script>
(() => {
    const root = document.getElementById("startup");
    const statusUrl = root.dataset.statusUrl;
    const logs = document.getElementById("logs");
    const message = document.getElementById("message");
    const headline = document.getElementById("headline");
    const spinner = document.getElementById("spinner");
    const startedAt = Date.now();
    let attempts = 0;
    let opening = false;

    const setPill = (id, text, state = "") => {
        const node = document.getElementById(id);
        node.textContent = text;
        node.className = `metric-value pill ${state}`.trim();
    };

    const updateElapsed = () => {
        const seconds = Math.floor((Date.now() - startedAt) / 1000);
        document.getElementById("elapsed").textContent = `${seconds}s`;
        if (seconds >= 60 && !opening) {
            message.textContent = "Startup is taking longer than expected. Check the latest container output below for the blocking step.";
        }
    };

    async function refreshStatus() {
        attempts += 1;
        document.getElementById("attempts").textContent = String(attempts);
        try {
            const response = await fetch(statusUrl, { credentials: "same-origin", cache: "no-store" });
            if (!response.ok) throw new Error(`diagnostics returned HTTP ${response.status}`);
            const data = await response.json();
            const containerReady = data.container_status === "running";
            setPill("workspace-status", data.workspace_status, data.workspace_status === "running" ? "ready" : "error");
            setPill("container-status", data.container_status, containerReady ? "ready" : "error");
            setPill("port-status", data.port_ready ? `reachable :${data.host_port}` : `waiting :${data.host_port}`, data.port_ready ? "ready" : "");
            logs.textContent = data.logs || "Container is running but has not emitted output yet.";
            logs.scrollTop = logs.scrollHeight;
            if (data.error_message) {
                message.textContent = `Workspace error: ${data.error_message}`;
            }

            if (data.port_ready && containerReady && !opening) {
                opening = true;
                headline.textContent = "IDE is ready";
                message.textContent = "Connection established. Opening the workspace...";
                spinner.style.animationDuration = ".35s";
                setTimeout(() => window.location.reload(), 650);
            }
        } catch (error) {
            setPill("port-status", "diagnostics unavailable", "error");
            message.textContent = `Could not refresh diagnostics: ${error.message}. Retrying automatically.`;
        }
    }

    document.getElementById("retry").addEventListener("click", () => window.location.reload());
    updateElapsed();
    refreshStatus();
    setInterval(updateElapsed, 1000);
    setInterval(refreshStatus, 2000);
})();
</script>
</body>
</html>"""


def render_starting_page(workspace: Workspace) -> str:
    """Render a token-free, live startup diagnostics page."""
    return (
        STARTING_PAGE_HTML
        .replace("__WORKSPACE_NAME__", escape(workspace.name))
        .replace("__WORKSPACE_ID__", escape(workspace.id))
        .replace("__HOST_PORT__", str(workspace.host_port))
        .replace("__TEMPLATE_ID__", escape(workspace.template_id))
        .replace(
            "__STATUS_URL__",
            escape(f"/proxy/{workspace.id}/_devcloud/status?tail=120", quote=True),
        )
        .replace("__DETAIL_URL__", escape(f"/workspaces/{workspace.id}", quote=True))
    )


async def port_is_ready(host_port: int) -> bool:
    """Check whether the loopback-only IDE port is accepting TCP connections."""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", host_port),
            timeout=0.6,
        )
    except (OSError, TimeoutError):
        return False

    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True


@proxy_router.get("/{workspace_id}/_devcloud/status")
async def proxy_workspace_status(
    workspace_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
    response: Response,
    tail: int = 120,
):
    """Return authenticated startup diagnostics without exposing workspace tokens."""
    workspace = await get_authorized_workspace(
        workspace_id,
        db,
        current_user,
        require_running=False,
    )
    response.headers["Cache-Control"] = "no-store"
    safe_tail = max(20, min(tail, 200))
    container_status, ready, logs = await asyncio.gather(
        podman_service.get_container_status(workspace.container_name),
        port_is_ready(workspace.host_port),
        podman_service.get_logs(workspace.container_name, tail=safe_tail),
    )
    return {
        "workspace_id": workspace.id,
        "workspace_status": workspace.status.value,
        "container_status": container_status,
        "host_port": workspace.host_port,
        "port_ready": ready,
        "logs": logs,
        "error_message": workspace.error_message,
        "last_started_at": (
            workspace.last_started_at.isoformat() if workspace.last_started_at else None
        ),
    }


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
        except (httpx.ConnectError, httpx.ConnectTimeout):
            if attempt < max_retries - 1:
                await asyncio.sleep(1.0)
            else:
                await client.aclose()
                # Return a live diagnostics screen for browser navigation requests.
                if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
                    logger.info(
                        "Workspace %s is not accepting HTTP connections on port %s after %s attempts",
                        workspace.id,
                        workspace.host_port,
                        max_retries,
                    )
                    return HTMLResponse(
                        content=render_starting_page(workspace),
                        status_code=200,
                        headers={"Cache-Control": "no-store", "Retry-After": "2"},
                    )

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

    # Header dictionaries collapse repeated Set-Cookie fields into one invalid
    # comma-separated value. Preserve each cookie independently so Jupyter's
    # login and _xsrf cookies both reach the browser.
    if hasattr(resp.headers, "get_list"):
        set_cookie_headers = resp.headers.get_list("set-cookie")
    else:
        set_cookie_value = resp.headers.get("set-cookie")
        set_cookie_headers = [set_cookie_value] if set_cookie_value else []

    response_headers = dict(resp.headers)
    # Remove hop-by-hop headers and cookies handled through raw_headers below.
    for h in ["transfer-encoding", "content-length", "set-cookie"]:
        response_headers.pop(h, None)

    proxy_response = StreamingResponse(
        response_stream(),
        status_code=resp.status_code,
        headers=response_headers,
        media_type=resp.headers.get("content-type"),
    )
    for cookie_header in set_cookie_headers:
        proxy_response.raw_headers.append(
            (b"set-cookie", cookie_header.encode("latin-1"))
        )
    return proxy_response


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
