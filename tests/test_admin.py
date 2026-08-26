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
    assert "Genel Bakış" in admin_page.text
    assert 'class="admin-category-link is-active"' in admin_page.text
    assert 'href="/admin/users"' in admin_page.text
    assert "Kota Ayarları" not in admin_page.text

    users_page = await client.get("/admin/users", headers=admin_headers)
    assert users_page.status_code == 200
    assert "Kota Ayarları" in users_page.text
    assert 'class="admin-user-card"' in users_page.text
    assert 'class="quota-form admin-quota-form"' in users_page.text
    assert "Kurumsal Dizin (LDAPS / Active Directory)" in users_page.text
    assert 'id="directory-settings-form"' in users_page.text
    assert 'value="ldaps.tcmb.gov.tr"' in users_page.text

    workers_page = await client.get("/admin/workers", headers=admin_headers)
    assert workers_page.status_code == 200
    assert "Worker Node'ları" in workers_page.text
    assert 'id="node-create-form"' in workers_page.text

    integrations_page = await client.get(
        "/admin/integrations", headers=admin_headers
    )
    assert integrations_page.status_code == 200
    assert "MLflow Model Registry" in integrations_page.text
    assert 'id="mlflow-settings-form"' in integrations_page.text

    workspaces_page = await client.get("/admin/workspaces", headers=admin_headers)
    assert workspaces_page.status_code == 200
    assert "Tüm Container ve Çalışma Alanları" in workspaces_page.text
    assert 'id="btn-open-template-builder-modal"' in workspaces_page.text

    missing_page = await client.get("/admin/not-a-section", headers=admin_headers)
    assert missing_page.status_code == 404

    models_page = await client.get("/models", headers=admin_headers)
    assert models_page.status_code == 200
    assert "MLflow henüz etkin değil" in models_page.text
