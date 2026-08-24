import pytest
from httpx import AsyncClient


async def get_authenticated_headers(client: AsyncClient, username: str = "dev_user") -> dict[str, str]:
    """Helper to register and return authorization headers."""
    resp = await client.post(
        "/api/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "Password123!",
            "full_name": "Dev User",
        },
    )
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_view_routes_render_html(client: AsyncClient):
    """Test that HTML view routes render without 500 errors."""
    # 1. Login page
    login_resp = await client.get("/login")
    assert login_resp.status_code == 200
    assert "Welcome to DevCloud" in login_resp.text

    # 2. Register page
    reg_resp = await client.get("/register")
    assert reg_resp.status_code == 200
    assert "Create Your Account" in reg_resp.text

    # 3. Root redirect to /login for unauthenticated users
    root_resp = await client.get("/", follow_redirects=False)
    assert root_resp.status_code == 302
    assert root_resp.headers["location"] == "/login"

    # 4. Authenticated dashboard rendering
    headers = await get_authenticated_headers(client, "dashboard_view_user")
    auth_dashboard = await client.get("/", headers=headers)
    assert auth_dashboard.status_code == 200
    assert "My Workspaces" in auth_dashboard.text
    assert "Total System Usage" in auth_dashboard.text
    assert "Total User Usage" in auth_dashboard.text
    assert "Remaining User Quota" in auth_dashboard.text
    assert "Select Resource Flavor" in auth_dashboard.text

    # 5. Create a workspace and render dashboard + detail page
    create_payload = {
        "name": "Dashboard Template Test",
        "description": "Checking template render",
        "template_id": "vscode-empty",
        "flavor_id": "t1.nano",
    }
    ws_res = await client.post("/api/workspaces", json=create_payload, headers=headers)
    assert ws_res.status_code == 201
    ws_id = ws_res.json()["id"]

    # Render dashboard with existing workspace
    auth_dashboard_with_ws = await client.get("/", headers=headers)
    assert auth_dashboard_with_ws.status_code == 200
    assert "Dashboard Template Test" in auth_dashboard_with_ws.text

    usage_resp = await client.get("/api/workspaces/usage", headers=headers)
    assert usage_resp.status_code == 200
    usage = usage_resp.json()
    assert set(usage["system"]) == {"cpu", "memory", "disk"}
    assert usage["user"]["cpu"]["used"] == 0.5
    assert usage["user"]["memory"]["used_display"] == "512.0 MB"
    assert usage["user"]["cpu"]["remaining"] == 3.5

    # Render workspace detail page
    detail_resp = await client.get(f"/workspaces/{ws_id}", headers=headers)
    assert detail_resp.status_code == 200
    assert "Dashboard Template Test" in detail_resp.text
