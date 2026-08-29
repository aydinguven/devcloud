import pytest
from httpx import AsyncClient
from sqlalchemy import update

from app.models.user import User
from tests.conftest import TestingSessionLocal


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


@pytest.mark.asyncio
async def test_profile_page_is_read_only_and_shows_directory_fields(client: AsyncClient):
    reg_resp = await client.post(
        "/api/auth/register",
        json={
            "username": "directory_profile",
            "email": "directory@test.com",
            "password": "Password123!",
            "full_name": "Directory User",
        },
    )
    user_id = reg_resp.json()["user"]["id"]
    headers = {"Authorization": f"Bearer {reg_resp.json()['access_token']}"}
    async with TestingSessionLocal() as session:
        await session.execute(
            update(User)
            .where(User.id == user_id)
            .values(
                auth_source="active_directory",
                team="ML Platform",
                directorate="Yapay Zeka Müdürlüğü",
            )
        )
        await session.commit()

    page = await client.get("/profile", headers=headers)
    assert page.status_code == 200
    assert "directory_profile" in page.text
    assert "Directory User" in page.text
    assert "directory@test.com" in page.text
    assert "ML Platform" in page.text
    assert "Yapay Zeka Müdürlüğü" in page.text
    assert "Parolayı Değiştir" not in page.text
    assert 'id="profile-form"' not in page.text

    update_response = await client.put(
        "/api/auth/profile",
        headers=headers,
        json={"full_name": "Changed"},
    )
    assert update_response.status_code == 403
    assert "değiştirilemez" in update_response.json()["detail"]
