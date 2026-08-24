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
    standard_user_id = reg_user.json()["user"]["id"]
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

    forbidden_quota = await client.put(
        f"/api/admin/users/{standard_user_id}/quota",
        headers=user_headers,
        json={"cpu_quota": 2, "memory_mb_quota": 2048, "disk_mb_quota": 8192},
    )
    assert forbidden_quota.status_code == 403

    quota_resp = await client.put(
        f"/api/admin/users/{standard_user_id}/quota",
        headers=admin_headers,
        json={"cpu_quota": 2.5, "memory_mb_quota": 3072, "disk_mb_quota": 12288},
    )
    assert quota_resp.status_code == 200
    assert quota_resp.json()["cpu_quota"] == 2.5
    assert quota_resp.json()["memory_mb_quota"] == 3072
    assert quota_resp.json()["disk_mb_quota"] == 12288

    invalid_quota = await client.put(
        f"/api/admin/users/{standard_user_id}/quota",
        headers=admin_headers,
        json={"cpu_quota": -1, "memory_mb_quota": 1024, "disk_mb_quota": 1024},
    )
    assert invalid_quota.status_code == 422

    admin_page = await client.get("/admin", headers=admin_headers)
    assert admin_page.status_code == 200
    assert "Quota Controls" in admin_page.text
    assert 'class="quota-form"' in admin_page.text
