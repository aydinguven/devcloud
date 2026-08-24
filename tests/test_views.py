import pytest
from httpx import AsyncClient


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

    # Follow redirect to /login
    followed_resp = await client.get("/", follow_redirects=True)
    assert followed_resp.status_code == 200
    assert "Welcome to DevCloud" in followed_resp.text

    # 4. Authenticated dashboard rendering (tests dashboard.html compilation)
    reg_user = await client.post(
        "/api/auth/register",
        json={"username": "template_user", "email": "tu@test.com", "password": "Password123!"},
    )
    assert reg_user.status_code == 201

    dash_resp = await client.get("/")
    assert dash_resp.status_code == 200
    assert "Select Resource Flavor" in dash_resp.text
    assert "Select Development Environment Template" in dash_resp.text

    # 5. Profile page rendering
    prof_resp = await client.get("/profile")
    assert prof_resp.status_code == 200
    assert "Git Credentials Integration" in prof_resp.text



