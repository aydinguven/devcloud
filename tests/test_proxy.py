import pytest
from httpx import AsyncClient


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
