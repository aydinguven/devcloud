import pytest
from httpx import AsyncClient
from app.config import settings


@pytest.mark.asyncio
async def test_user_registration_and_login(client: AsyncClient):
    """Test user sign up, login, and retrieving profile."""
    # 1. Register
    reg_payload = {
        "username": "coder_alice",
        "email": "alice@example.com",
        "password": "SecurePassword123!",
        "full_name": "Alice Developer",
    }
    reg_resp = await client.post("/api/auth/register", json=reg_payload)
    assert reg_resp.status_code == 201, reg_resp.text
    data = reg_resp.json()
    assert data["user"]["username"] == "coder_alice"
    assert data["user"]["cpu_quota"] == 1.0
    assert data["user"]["disk_mb_quota"] == 10240
    assert data["user"]["memory_mb_quota"] == 1024
    assert "access_token" in data

    token = data["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # 2. Get current user profile
    me_resp = await client.get("/api/auth/me", headers=headers)
    assert me_resp.status_code == 200
    assert me_resp.json()["email"] == "alice@example.com"

    # 3. Login with credentials
    login_resp = await client.post(
        "/api/auth/login",
        json={"username": "coder_alice", "password": "SecurePassword123!"},
    )
    assert login_resp.status_code == 200
    assert "access_token" in login_resp.json()

    # 4. Login with invalid password
    bad_login = await client.post(
        "/api/auth/login",
        json={"username": "coder_alice", "password": "WrongPassword"},
    )
    assert bad_login.status_code == 401


@pytest.mark.asyncio
async def test_duplicate_user_registration(client: AsyncClient):
    """Test that registering duplicate usernames or emails is rejected."""
    payload = {
        "username": "duplicate_user",
        "email": "dup@example.com",
        "password": "password123",
        "full_name": "Duplicate User",
    }
    res1 = await client.post("/api/auth/register", json=payload)
    assert res1.status_code == 201

    # Duplicate username
    res2 = await client.post("/api/auth/register", json=payload)
    assert res2.status_code == 400
    assert "already taken" in res2.json()["detail"]


@pytest.mark.asyncio
async def test_secure_session_cookie_setting(client: AsyncClient, monkeypatch):
    """HTTPS deployments mark both issued and expired session cookies Secure."""
    monkeypatch.setattr(settings, "COOKIE_SECURE", True)
    register = await client.post(
        "/api/auth/register",
        json={
            "username": "secure_cookie_user",
            "email": "secure-cookie@example.com",
            "password": "SecurePassword123!",
            "full_name": "Secure Cookie",
        },
    )

    assert register.status_code == 201
    session_cookie = register.headers["set-cookie"]
    assert "HttpOnly" in session_cookie
    assert "SameSite=lax" in session_cookie
    assert "Secure" in session_cookie

    logout = await client.post("/api/auth/logout")
    assert "Secure" in logout.headers["set-cookie"]
