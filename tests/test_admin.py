import pytest
from httpx import AsyncClient
from app.auth.internal import create_access_token


@pytest.mark.asyncio
async def test_admin_access_controls(client: AsyncClient):
    """Test admin endpoint access restrictions."""
    # 1. Normal user cannot access admin stats
    reg_user = await client.post(
        "/api/auth/register",
        json={"username": "standard_user", "email": "std@test.com", "password": "Password123!"},
    )
    user_token = reg_user.json()["access_token"]
    user_headers = {"Authorization": f"Bearer {user_token}"}

    forbidden_resp = await client.get("/api/admin/stats", headers=user_headers)
    assert forbidden_resp.status_code == 403

    # 2. Register admin user
    from app.auth.internal import hash_password
    from app.models.user import User, UserRole
    from app.database import get_db

    # Create admin user via direct registration and update role or create in db
    admin_reg = await client.post(
        "/api/auth/register",
        json={"username": "superadmin", "email": "admin@test.com", "password": "AdminPassword123!"},
    )
    admin_token = admin_reg.json()["access_token"]
    admin_id = admin_reg.json()["user"]["id"]

    # We update user role to ADMIN in DB
    from tests.conftest import TestingSessionLocal
    from sqlalchemy import update
    async with TestingSessionLocal() as session:
        await session.execute(update(User).where(User.id == admin_id).values(role=UserRole.ADMIN))
        await session.commit()

    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    admin_resp = await client.get("/api/admin/stats", headers=admin_headers)
    assert admin_resp.status_code == 200
    assert admin_resp.json()["total_users"] >= 2
