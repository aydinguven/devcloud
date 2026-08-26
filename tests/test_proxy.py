import gzip

import pytest
from httpx import AsyncClient

import app.proxy.router as proxy_module
from app.agents.manager import AgentStream, StreamChunk
from app.models.workspace import Workspace
from sqlalchemy import update
from tests.conftest import TestingSessionLocal

@pytest.mark.asyncio
async def test_proxy_auth_guard(client: AsyncClient):
    """Test that unauthorized requests to proxy routes are blocked."""
    # Attempt to proxy without authentication
    resp = await client.get("/proxy/non-existent-ws/index.html")
    assert resp.status_code == 404 or resp.status_code == 401


@pytest.mark.asyncio
async def test_proxy_unauthorized_user_access(client: AsyncClient):
    """Test that User B cannot access User A's workspace."""
    # Register User A
    user_a_res = await client.post(
        "/api/auth/register",
        json={"username": "user_a", "email": "a@test.com", "password": "Password123!"},
    )
    token_a = user_a_res.json()["access_token"]

    # User A creates workspace
    ws_res = await client.post(
        "/api/workspaces",
        json={"name": "Alice WS", "template_id": "vscode-empty", "flavor_id": "t1.nano"},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    ws_id = ws_res.json()["id"]

    # Register User B
    user_b_res = await client.post(
        "/api/auth/register",
        json={"username": "user_b", "email": "b@test.com", "password": "Password123!"},
    )
    token_b = user_b_res.json()["access_token"]

    # User B attempts to access User A's workspace
    forbidden_res = await client.get(
        f"/proxy/{ws_id}/",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert forbidden_res.status_code == 403


@pytest.mark.asyncio
async def test_proxy_waiting_page_exposes_live_authenticated_diagnostics(
    client: AsyncClient,
    monkeypatch,
):
    """A slow IDE should show live, owner-only container and port diagnostics."""
    register = await client.post(
        "/api/auth/register",
        json={
            "username": "proxy_startup_logs_user",
            "email": "proxy-startup-logs@test.com",
            "password": "Password123!",
        },
    )
    token = register.json()["access_token"]
    workspace_response = await client.post(
        "/api/workspaces",
        json={
            "name": "Logging IDE",
            "template_id": "jupyter-python",
            "flavor_id": "t1.nano",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    workspace = workspace_response.json()

    class UnavailableProxyClient:
        def __init__(self, *args, **kwargs):
            return None

        def build_request(self, *args, **kwargs):
            return proxy_module.httpx.Request("GET", "http://127.0.0.1/")

        async def send(self, request, stream=False):
            raise proxy_module.httpx.ConnectError("IDE is not ready", request=request)

        async def aclose(self):
            return None

    async def no_sleep(_seconds):
        return None

    async def port_not_ready(_host_port):
        return False

    monkeypatch.setattr(proxy_module.httpx, "AsyncClient", UnavailableProxyClient)
    monkeypatch.setattr(proxy_module.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(proxy_module, "port_is_ready", port_not_ready)

    response = await client.get(
        f"/proxy/{workspace['id']}/",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "text/html",
        },
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "Son Container Çıktısı" in response.text
    assert "IDE Portu" in response.text
    assert "Kontrol" in response.text
    assert f"/proxy/{workspace['id']}/_devcloud/status?tail=120" in response.text
    assert "http-equiv=\"refresh\"" not in response.text

    status_response = await client.get(
        f"/proxy/{workspace['id']}/_devcloud/status?tail=120",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert status_response.status_code == 200
    diagnostics = status_response.json()
    assert diagnostics["workspace_status"] == "running"
    assert diagnostics["container_status"] == "running"
    assert diagnostics["host_port"] == workspace["host_port"]
    assert diagnostics["port_ready"] is False
    assert "başlatılıyor" in diagnostics["logs"]

    other_register = await client.post(
        "/api/auth/register",
        json={
            "username": "proxy_startup_logs_other_user",
            "email": "proxy-startup-logs-other@test.com",
            "password": "Password123!",
        },
    )
    other_token = other_register.json()["access_token"]
    forbidden = await client.get(
        f"/proxy/{workspace['id']}/_devcloud/status",
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert forbidden.status_code == 403


@pytest.mark.asyncio
async def test_proxy_preserves_multiple_jupyter_set_cookie_headers(
    client: AsyncClient,
    monkeypatch,
):
    """Jupyter's login and XSRF cookies must reach the browser independently."""
    register = await client.post(
        "/api/auth/register",
        json={
            "username": "proxy_cookie_user",
            "email": "proxy-cookie@test.com",
            "password": "Password123!",
        },
    )
    token = register.json()["access_token"]
    workspace_response = await client.post(
        "/api/workspaces",
        json={
            "name": "Cookie IDE",
            "template_id": "jupyter-python",
            "flavor_id": "t1.nano",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    workspace_id = workspace_response.json()["id"]
    cookie_path = f"/proxy/{workspace_id}/"
    upstream_cookies = [
        f"_xsrf=xsrf-value; Path={cookie_path}",
        f"username-jupyter=login-value; HttpOnly; Path={cookie_path}",
    ]

    class CookieUpstreamResponse:
        status_code = 200
        headers = proxy_module.httpx.Headers(
            [
                ("content-type", "text/html; charset=utf-8"),
                ("set-cookie", upstream_cookies[0]),
                ("set-cookie", upstream_cookies[1]),
            ]
        )

        async def aiter_raw(self):
            yield b"<html>JupyterLab</html>"

        async def aclose(self):
            return None

    class CookieProxyClient:
        def __init__(self, *args, **kwargs):
            return None

        def build_request(self, *args, **kwargs):
            return object()

        async def send(self, request, stream=False):
            assert stream is True
            return CookieUpstreamResponse()

        async def aclose(self):
            return None

    monkeypatch.setattr(proxy_module.httpx, "AsyncClient", CookieProxyClient)

    response = await client.get(
        f"/proxy/{workspace_id}/",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.headers.get_list("set-cookie") == upstream_cookies


@pytest.mark.asyncio
async def test_proxy_preserves_content_encoding_for_raw_stream(client: AsyncClient, monkeypatch):
    """Compressed bytes and Jupyter browser-facing headers must survive proxying."""
    html_body = b"<!doctype html><html><body>Code Server</body></html>"
    compressed_body = gzip.compress(html_body)

    captured_request = {}
    class FakeUpstreamResponse:
        status_code = 200
        headers = {
            "content-type": "text/html; charset=utf-8",
            "content-encoding": "gzip",
            "content-length": str(len(compressed_body)),
        }

        async def aiter_raw(self):
            yield compressed_body

        async def aclose(self):
            return None

    class FakeProxyClient:
        def __init__(self, *args, **kwargs):
            self.response = FakeUpstreamResponse()

        def build_request(self, *args, **kwargs):
            captured_request.update(kwargs)
            return object()

        async def send(self, request, stream=False):
            assert stream is True
            return self.response

        async def aclose(self):
            return None

    monkeypatch.setattr(proxy_module.httpx, "AsyncClient", FakeProxyClient)

    register = await client.post(
        "/api/auth/register",
        json={
            "username": "proxy_compression_user",
            "email": "proxy-compression@test.com",
            "password": "Password123!",
        },
    )
    token = register.json()["access_token"]
    workspace = await client.post(
        "/api/workspaces",
        json={
            "name": "Compressed IDE",
            "template_id": "jupyter-python",
            "flavor_id": "t1.nano",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    workspace_id = workspace.json()["id"]

    response = await client.get(
        f"/proxy/{workspace_id}/",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept-Encoding": "gzip",
            "Origin": "http://test",
            "Cookie": f"devcloud_session={token}; _xsrf=workspace-xsrf",
        },
    )

    assert response.status_code == 200
    assert response.content == html_body
    assert response.headers["content-encoding"] == "gzip"
    assert captured_request["url"].endswith(f"/proxy/{workspace_id}/")
    captured_headers = {
        key.lower(): value for key, value in captured_request["headers"].items()
    }
    assert captured_headers["authorization"].startswith("token ")
    assert captured_headers["authorization"] != f"Bearer {token}"
    assert captured_headers["origin"] == "http://test"
    assert captured_headers["host"] == "test"
    assert captured_headers["x-forwarded-host"] == "test"
    assert captured_headers["x-forwarded-proto"] == "http"
    assert captured_headers["cookie"] == "_xsrf=workspace-xsrf"
    assert token not in captured_headers["cookie"]


@pytest.mark.asyncio
async def test_custom_port_path_is_dispatched_before_catch_all_proxy(client: AsyncClient, monkeypatch):
    register = await client.post(
        "/api/auth/register",
        json={"username": "custom_port_user", "email": "custom-port@test.com", "password": "Password123!"},
    )
    token = register.json()["access_token"]
    workspace = await client.post(
        "/api/workspaces",
        json={"name": "Custom Port", "template_id": "vscode-empty", "flavor_id": "t1.nano"},
        headers={"Authorization": f"Bearer {token}"},
    )
    workspace_id = workspace.json()["id"]
    captured = {}

    class Upstream:
        status_code = 200
        headers = {"content-type": "application/json"}
        async def aiter_raw(self):
            yield b'{"ok":true}'
        async def aclose(self):
            return None

    class Client:
        def __init__(self, *args, **kwargs):
            return None
        def build_request(self, **kwargs):
            captured.update(kwargs)
            return object()
        async def send(self, request, stream=False):
            return Upstream()
        async def aclose(self):
            return None

    async def container_ip(_name):
        return "10.88.0.42"

    monkeypatch.setattr(proxy_module.httpx, "AsyncClient", Client)
    monkeypatch.setattr(proxy_module.podman_service, "get_container_ip", container_ip)
    response = await client.get(
        f"/proxy/{workspace_id}/port/5173/api/health?verbose=1",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert captured["url"] == "http://10.88.0.42:5173/api/health?verbose=1"


@pytest.mark.asyncio
async def test_remote_custom_port_uses_worker_tunnel_and_same_public_url(client: AsyncClient, monkeypatch):
    register = await client.post(
        "/api/auth/register",
        json={"username": "remote_proxy_user", "email": "remote-proxy@test.com", "password": "Password123!"},
    )
    token = register.json()["access_token"]
    workspace = await client.post(
        "/api/workspaces",
        json={"name": "Remote Port", "template_id": "vscode-empty", "flavor_id": "t1.nano"},
        headers={"Authorization": f"Bearer {token}"},
    )
    workspace_id = workspace.json()["id"]
    async with TestingSessionLocal() as session:
        await session.execute(update(Workspace).where(Workspace.id == workspace_id).values(node_id="remote-node"))
        await session.commit()

    captured = {}

    class Connection:
        async def open_stream(self, action, payload):
            captured["action"] = action
            captured["payload"] = payload
            stream = AgentStream("stream-1")
            await stream.queue.put(StreamChunk(b"remote-ok"))
            await stream.queue.put(None)
            return {"status_code": 200, "headers": [["content-type", "text/plain"]]}, stream

    monkeypatch.setattr(proxy_module.agent_manager, "get", lambda node_id: Connection())
    response = await client.get(
        f"/proxy/{workspace_id}/port/3000/health",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert response.text == "remote-ok"
    assert captured["action"] == "proxy.http.open"
    assert captured["payload"]["custom_port"] == 3000
    assert captured["payload"]["path"] == "/health"
