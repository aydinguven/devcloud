import gzip

import pytest
from httpx import AsyncClient

import app.proxy.router as proxy_module

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
async def test_proxy_preserves_content_encoding_for_raw_stream(client: AsyncClient, monkeypatch):
    """Compressed upstream bytes must retain their Content-Encoding header."""
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
        headers={"Authorization": f"Bearer {token}", "Accept-Encoding": "gzip"},
    )

    assert response.status_code == 200
    assert response.content == html_body
    assert response.headers["content-encoding"] == "gzip"
    assert captured_request["url"].endswith(f"/proxy/{workspace_id}/")
    assert captured_request["headers"]["Authorization"].startswith("token ")
    assert captured_request["headers"]["Authorization"] != f"Bearer {token}"
