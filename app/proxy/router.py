import asyncio
import base64
import logging
from html import escape
from http.cookies import SimpleCookie
from typing import Annotated
from urllib.parse import parse_qsl, urlencode
from fastapi import APIRouter, Depends, HTTPException, Request, Response, WebSocket, WebSocketDisconnect, status
from fastapi.responses import HTMLResponse, StreamingResponse
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import websockets

from app.auth.dependencies import get_current_user_optional
from app.config import settings
from app.database import get_db
from app.models.user import User, UserRole
from app.models.workspace import Workspace, WorkspaceStatus
from app.orchestrator.podman_service import podman_service
from app.orchestrator.runtime_backend import runtime_for_node
from app.agents.manager import AgentCommandError, AgentUnavailable, agent_manager

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
        raise HTTPException(status_code=404, detail="Çalışma alanı bulunamadı.")

    if not current_user:
        raise HTTPException(status_code=401, detail="Çalışma alanına erişmek için oturum açmalısınız.")

    if workspace.user_id != current_user.id and current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Bu çalışma alanı için erişim reddedildi.")

    if require_running and workspace.status != WorkspaceStatus.RUNNING:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Çalışma alanı çalışmıyor (mevcut durum: {workspace.status}). Önce çalışma alanını başlatın.",
        )

    return workspace


def get_upstream_path(workspace_id: str, template_id: str, path: str) -> str:
    """Keep Jupyter's configured proxy prefix while other IDEs use root paths."""
    normalized_path = path.lstrip("/")
    if "jupyter" in template_id:
        return f"/proxy/{workspace_id}/{normalized_path}"
    return f"/{normalized_path}"


STARTING_PAGE_HTML = """<!DOCTYPE html>
<html lang="tr">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>__WORKSPACE_NAME__ başlatılıyor - DevCloud</title>
    <style>
        :root { color-scheme: light; font-family: "Segoe UI", Arial, sans-serif; color: #323a47; }
        * { box-sizing: border-box; }
        body { min-height: 100vh; margin: 0; padding: 28px; background: #e6ecf5; color: #323a47; border-top: 5px solid #d50032; }
        .shell { width: min(940px, 100%); margin: 0 auto; }
        .header { display: flex; align-items: center; gap: 16px; margin-bottom: 20px; }
        .spinner { flex: 0 0 auto; width: 42px; height: 42px; border: 4px solid #cfd7e5; border-top-color: #d50032; border-radius: 50%; animation: spin 1s linear infinite; }
        @keyframes spin { to { transform: rotate(360deg); } }
        h1 { margin: 0 0 4px; font-size: clamp(1.3rem, 3vw, 1.8rem); }
        .subtitle { margin: 0; color: #667085; }
        .panel { background: #fff; border: 1px solid #cfd7e5; border-radius: 2px; box-shadow: 0 10px 30px rgba(50, 58, 71, .12); overflow: hidden; }
        .status-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 1px; background: #cfd7e5; border-bottom: 1px solid #cfd7e5; }
        .metric { min-height: 88px; padding: 16px; background: #f8fafc; }
        .metric-label { display: block; margin-bottom: 9px; color: #667085; font-size: .72rem; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }
        .metric-value { color: #323a47; font-size: .94rem; font-weight: 650; overflow-wrap: anywhere; }
        .pill { display: inline-flex; align-items: center; gap: 7px; }
        .pill::before { content: ""; width: 8px; height: 8px; border-radius: 50%; background: #a3953b; }
        .pill.ready::before { background: #22c55e; }
        .pill.error::before { background: #d50032; }
        .log-head { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 14px 18px; border-bottom: 1px solid #cfd7e5; }
        .log-head strong { font-size: .88rem; }
        .log-head span { color: #667085; font-size: .76rem; }
        pre { min-height: 280px; max-height: 48vh; margin: 0; padding: 18px; overflow: auto; background: #323a47; color: #f8fafc; font: 12px/1.55 "SFMono-Regular", Consolas, "Liberation Mono", monospace; white-space: pre-wrap; overflow-wrap: anywhere; }
        .footer { display: flex; align-items: center; justify-content: space-between; gap: 14px; padding: 16px 18px; }
        .message { margin: 0; color: #667085; font-size: .84rem; }
        .actions { display: flex; gap: 9px; flex-wrap: wrap; }
        button, a { border: 1px solid #323a47; border-radius: 2px; padding: 8px 12px; background: #323a47; color: #fff; font: inherit; font-size: .82rem; text-decoration: none; cursor: pointer; }
        button:hover, a:hover { border-color: #d50032; background: #d50032; color: #fff; }
        code { color: #d50032; }
        @media (max-width: 620px) { body { padding: 18px 12px; } .footer { align-items: flex-start; flex-direction: column; } pre { min-height: 230px; } }
    </style>
</head>
<body>
<main class="shell" id="startup" data-status-url="__STATUS_URL__">
    <div class="header">
        <div class="spinner" id="spinner"></div>
        <div>
            <h1 id="headline">__WORKSPACE_NAME__ başlatılıyor...</h1>
            <p class="subtitle">IDE servisinin <code>__HOST_PORT__</code> host portunda hazır olması bekleniyor.</p>
        </div>
    </div>
    <section class="panel" aria-live="polite">
        <div class="status-grid">
            <div class="metric"><span class="metric-label">Çalışma Alanı</span><span class="metric-value pill" id="workspace-status">çalışıyor</span></div>
            <div class="metric"><span class="metric-label">Container</span><span class="metric-value pill" id="container-status">kontrol ediliyor</span></div>
            <div class="metric"><span class="metric-label">IDE Portu</span><span class="metric-value pill" id="port-status">kontrol ediliyor :__HOST_PORT__</span></div>
            <div class="metric"><span class="metric-label">Geçen Süre</span><span class="metric-value" id="elapsed">0 sn</span></div>
            <div class="metric"><span class="metric-label">Kontrol</span><span class="metric-value" id="attempts">0</span></div>
        </div>
        <div class="log-head"><strong>Son Container Çıktısı</strong><span>__TEMPLATE_ID__ · 2 saniyede bir yenilenir</span></div>
        <pre id="logs">Container logları alınıyor...</pre>
        <div class="footer">
            <p class="message" id="message">IDE portu bağlantı kabul ettiğinde sayfa otomatik olarak açılacaktır.</p>
            <div class="actions">
                <button type="button" id="retry">Şimdi Yeniden Dene</button>
                <a href="__DETAIL_URL__">Çalışma Alanı Ayrıntıları</a>
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
        document.getElementById("elapsed").textContent = `${seconds} sn`;
        if (seconds >= 60 && !opening) {
            message.textContent = "Başlatma beklenenden uzun sürüyor. Takılan adım için aşağıdaki son Container çıktısını inceleyin.";
        }
    };

    async function refreshStatus() {
        attempts += 1;
        document.getElementById("attempts").textContent = String(attempts);
        try {
            const response = await fetch(statusUrl, { credentials: "same-origin", cache: "no-store" });
            if (!response.ok) throw new Error(`tanılama HTTP ${response.status} döndürdü`);
            const data = await response.json();
            const containerReady = data.container_status === "running";
            setPill("workspace-status", data.workspace_status === "running" ? "çalışıyor" : data.workspace_status, data.workspace_status === "running" ? "ready" : "error");
            setPill("container-status", containerReady ? "çalışıyor" : data.container_status, containerReady ? "ready" : "error");
            setPill("port-status", data.port_ready ? `erişilebilir :${data.host_port}` : `bekleniyor :${data.host_port}`, data.port_ready ? "ready" : "");
            logs.textContent = data.logs || "Container çalışıyor ancak henüz çıktı üretmedi.";
            logs.scrollTop = logs.scrollHeight;
            if (data.error_message) {
                message.textContent = `Çalışma alanı hatası: ${data.error_message}`;
            }

            if (data.port_ready && containerReady && !opening) {
                opening = true;
                headline.textContent = "IDE hazır";
                message.textContent = "Bağlantı kuruldu. Çalışma alanı açılıyor...";
                spinner.style.animationDuration = ".35s";
                setTimeout(() => window.location.reload(), 650);
            }
        } catch (error) {
            setPill("port-status", "tanılama kullanılamıyor", "error");
            message.textContent = `Tanılama yenilenemedi: ${error.message}. Otomatik olarak yeniden deneniyor.`;
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


def _forward_headers(request: Request, workspace: Workspace, custom_port: int | None = None) -> dict[str, str]:
    excluded = {"host", "content-length", "connection", "authorization", "cookie"}
    headers = {key: value for key, value in request.headers.items() if key.lower() not in excluded}
    incoming_cookie = request.headers.get("cookie", "")
    if incoming_cookie:
        parsed = SimpleCookie()
        parsed.load(incoming_cookie)
        parsed.pop(settings.COOKIE_NAME, None)
        if parsed:
            headers["Cookie"] = "; ".join(
                f"{name}={morsel.value}" for name, morsel in parsed.items()
            )
    if custom_port is None and "jupyter" in workspace.template_id:
        public_host = request.headers.get("host")
        if public_host:
            headers["Host"] = public_host
            headers["X-Forwarded-Host"] = public_host
        headers["X-Forwarded-Proto"] = request.url.scheme
        headers["Authorization"] = f"token {workspace.workspace_token}"
    return headers


def _workspace_websocket_query(websocket: WebSocket) -> str:
    """Do not forward the DevCloud query-token credential into the container."""
    raw = websocket.scope.get("query_string", b"").decode()
    return urlencode([(key, value) for key, value in parse_qsl(raw, keep_blank_values=True) if key != "token"])


async def proxy_remote_http(
    workspace: Workspace,
    request: Request,
    path: str,
    custom_port: int | None = None,
):
    """Stream an HTTP request through the worker-initiated tunnel."""
    if not workspace.node_id:
        raise RuntimeError("Remote proxy için node_id gereklidir.")
    connection = agent_manager.get(workspace.node_id)
    payload = {
        "workspace_id": workspace.id,
        "container_name": workspace.container_name,
        "host_port": workspace.host_port,
        "custom_port": custom_port,
        "method": request.method,
        "path": path,
        "query": request.url.query,
        "headers": _forward_headers(request, workspace, custom_port),
        "body": base64.b64encode(await request.body()).decode("ascii"),
    }
    try:
        metadata, stream = await connection.open_stream("proxy.http.open", payload)
    except (AgentUnavailable, AgentCommandError, TimeoutError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    header_pairs = metadata.get("headers") or []
    response_headers: dict[str, str] = {}
    set_cookies: list[str] = []
    for key, value in header_pairs:
        lower = key.lower()
        if lower == "set-cookie":
            set_cookies.append(value)
        elif lower not in {"transfer-encoding", "content-length", "connection"}:
            response_headers[key] = value

    async def response_stream():
        while True:
            item = await stream.queue.get()
            if item is None:
                break
            if isinstance(item, Exception):
                raise item
            yield item.data

    response = StreamingResponse(
        response_stream(),
        status_code=int(metadata.get("status_code", 502)),
        headers=response_headers,
        media_type=response_headers.get("content-type"),
    )
    for cookie in set_cookies:
        response.raw_headers.append((b"set-cookie", cookie.encode("latin-1")))
    return response


async def proxy_remote_websocket(
    websocket: WebSocket,
    workspace: Workspace,
    path: str,
    custom_port: int | None = None,
) -> None:
    if not workspace.node_id:
        raise RuntimeError("Remote proxy için node_id gereklidir.")
    connection = agent_manager.get(workspace.node_id)
    headers = {}
    if custom_port is None and "jupyter" in workspace.template_id:
        headers["Authorization"] = f"token {workspace.workspace_token}"
    metadata, stream = await connection.open_stream(
        "proxy.websocket.open",
        {
            "workspace_id": workspace.id,
            "container_name": workspace.container_name,
            "host_port": workspace.host_port,
            "custom_port": custom_port,
            "path": path,
            "query": _workspace_websocket_query(websocket),
            "headers": headers,
        },
    )
    if not metadata.get("connected"):
        raise AgentCommandError("Worker WebSocket hedefine bağlanamadı.")

    async def forward_to_worker():
        try:
            while True:
                message = await websocket.receive()
                if message.get("text") is not None:
                    await connection.send_stream_data(
                        stream.id, message["text"].encode("utf-8"), text=True
                    )
                elif message.get("bytes") is not None:
                    await connection.send_stream_data(stream.id, message["bytes"])
                elif message.get("type") == "websocket.disconnect":
                    break
        finally:
            await connection.close_stream(stream.id)

    async def forward_to_client():
        while True:
            item = await stream.queue.get()
            if item is None:
                break
            if isinstance(item, Exception):
                raise item
            if item.is_text:
                await websocket.send_text(item.data.decode("utf-8"))
            else:
                await websocket.send_bytes(item.data)

    await asyncio.gather(forward_to_worker(), forward_to_client(), return_exceptions=True)


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
    runtime = runtime_for_node(workspace.node_id)
    try:
        container_status, ready, logs = await asyncio.gather(
            runtime.get_container_status(workspace.container_name),
            runtime.port_ready(workspace.container_name, workspace.host_port),
            runtime.get_logs(workspace.container_name, tail=safe_tail),
        )
    except AgentUnavailable:
        container_status, ready, logs = "worker-offline", False, "Worker tunnel bağlantısı yok."
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
    custom_parts = path.split("/", 2)
    if len(custom_parts) >= 2 and custom_parts[0] == "port" and custom_parts[1].isdigit():
        return await proxy_custom_port_http(
            workspace_id=workspace_id,
            port=int(custom_parts[1]),
            request=request,
            db=db,
            current_user=current_user,
            path=custom_parts[2] if len(custom_parts) == 3 else "",
        )
    upstream_path = get_upstream_path(workspace.id, workspace.template_id, path)
    if workspace.node_id:
        return await proxy_remote_http(workspace, request, upstream_path)
    target_url = f"http://127.0.0.1:{workspace.host_port}{upstream_path}"
    
    if request.url.query:
        target_url = f"{target_url}?{request.url.query}"

    # Filter headers to pass through
    headers = _forward_headers(request, workspace)
    if "jupyter" in workspace.template_id:
        # Keep Jupyter's same-origin check intact. The TCP hop is loopback, but
        # Origin must be compared with the browser-facing Host rather than the
        # container port or Jupyter masks unsafe requests as a 404.
        public_host = request.headers.get("host")
        if public_host:
            headers["Host"] = public_host
            headers["X-Forwarded-Host"] = public_host
        else:
            headers["Host"] = f"127.0.0.1:{workspace.host_port}"
        headers["X-Forwarded-Proto"] = request.url.scheme
    else:
        headers["Host"] = f"127.0.0.1:{workspace.host_port}"

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
                    detail="Çalışma alanı Container servisine bağlanılamadı. Servis hâlâ başlatılıyor olabilir.",
                )
        except Exception as e:
            await client.aclose()
            logger.error(f"Proxy error for workspace {workspace_id}: {e}")
            raise HTTPException(status_code=500, detail=f"Proxy hatası: {str(e)}")

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
    token = websocket.query_params.get("token") or websocket.cookies.get(settings.COOKIE_NAME)
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

    custom_port = None
    custom_parts = path.split("/", 2)
    if len(custom_parts) >= 2 and custom_parts[0] == "port" and custom_parts[1].isdigit():
        custom_port = int(custom_parts[1])
        upstream_path = "/" + (custom_parts[2] if len(custom_parts) == 3 else "")
    else:
        upstream_path = get_upstream_path(workspace.id, workspace.template_id, path)

    if workspace.node_id:
        try:
            await proxy_remote_websocket(
                websocket,
                workspace,
                upstream_path,
                custom_port=custom_port,
            )
        except Exception as exc:
            logger.warning("Remote WebSocket proxy closed: %s", exc)
        finally:
            try:
                await websocket.close()
            except Exception:
                pass
        return

    target_host = "127.0.0.1"
    target_port = workspace.host_port
    if custom_port is not None:
        container_ip = await podman_service.get_container_ip(workspace.container_name)
        if not container_ip:
            await websocket.close(code=1011, reason="Container IP adresi bulunamadı")
            return
        target_host = container_ip
        target_port = custom_port
    target_ws_url = f"ws://{target_host}:{target_port}{upstream_path}"
    forwarded_query = _workspace_websocket_query(websocket)
    if forwarded_query:
        target_ws_url += f"?{forwarded_query}"

    additional_headers = None
    if custom_port is None and "jupyter" in workspace.template_id:
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


@proxy_router.api_route(
    "/{workspace_id}/port/{port}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
)
@proxy_router.api_route(
    "/{workspace_id}/port/{port}/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
)
async def proxy_custom_port_http(
    workspace_id: str,
    port: int,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
    path: str = "",
):
    """Proxy HTTP traffic to a custom secondary port running inside the container (e.g. 5173, 5000, 3000)."""
    workspace = await get_authorized_workspace(workspace_id, db, current_user)

    subpath = f"/{path.lstrip('/')}"
    if workspace.node_id:
        return await proxy_remote_http(
            workspace,
            request,
            subpath,
            custom_port=port,
        )

    container_ip = await podman_service.get_container_ip(workspace.container_name)
    target_host = container_ip if container_ip else "127.0.0.1"

    target_url = f"http://{target_host}:{port}{subpath}"
    if request.url.query:
        target_url += f"?{request.url.query}"

    headers = {
        k: v for k, v in request.headers.items() if k.lower() not in {"host", "connection"}
    }
    headers["Host"] = f"{target_host}:{port}"
    body = await request.body()

    client = httpx.AsyncClient(timeout=30.0, follow_redirects=False)
    try:
        req = client.build_request(
            method=request.method,
            url=target_url,
            headers=headers,
            content=body,
        )
        upstream_resp = await client.send(req, stream=True)
        resp_headers = {
            k: v
            for k, v in upstream_resp.headers.items()
            if k.lower() not in {"transfer-encoding", "connection", "content-length"}
        }

        async def stream_body():
            try:
                async for chunk in upstream_resp.aiter_raw():
                    yield chunk
            finally:
                await upstream_resp.aclose()
                await client.aclose()

        return StreamingResponse(
            stream_body(),
            status_code=upstream_resp.status_code,
            headers=resp_headers,
            media_type=upstream_resp.headers.get("content-type"),
        )
    except httpx.ConnectError:
        await client.aclose()
        raise HTTPException(
            status_code=502,
            detail=f"Could not connect to port {port} inside container '{workspace.name}'. Ensure your app is listening on 0.0.0.0:{port}.",
        )
    except Exception as exc:
        await client.aclose()
        raise HTTPException(status_code=500, detail=f"Custom port proxy error: {str(exc)}")
