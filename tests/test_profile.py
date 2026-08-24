import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_update_profile_and_change_password(client: AsyncClient):
    """Test updating user profile details and password changes."""
    # 1. Register
    reg_resp = await client.post(
        "/api/auth/register",
        json={"username": "profile_user", "email": "pu@test.com", "password": "OldPassword123!"},
    )
    token = reg_resp.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # 2. Update full name and change password
    update_resp = await client.put(
        "/api/auth/profile",
        json={"full_name": "Updated Name", "password": "NewPassword456!"},
        headers=headers,
    )
    assert update_resp.status_code == 200
    assert update_resp.json()["full_name"] == "Updated Name"

    # 3. Log in with new password
    login_new = await client.post(
        "/api/auth/login",
        json={"username": "profile_user", "password": "NewPassword456!"},
    )
    assert login_new.status_code == 200

    # 4. Old password should fail
    login_old = await client.post(
        "/api/auth/login",
        json={"username": "profile_user", "password": "OldPassword123!"},
    )
    assert login_old.status_code == 401

    # 5. Update Git Credentials
    git_update_resp = await client.put(
        "/api/auth/profile",
        json={
            "git_name": "Aydin Guven",
            "git_email": "aydin@aydin.cloud",
            "git_username": "aydin",
            "git_token": "glpat-secrettoken123",
            "git_server": "git.aydin.cloud",
        },
        headers={"Authorization": f"Bearer {login_new.json()['access_token']}"},
    )
    assert git_update_resp.status_code == 200
    git_data = git_update_resp.json()
    assert git_data["git_name"] == "Aydin Guven"
    assert git_data["git_username"] == "aydin"
    assert git_data["git_server"] == "git.aydin.cloud"

