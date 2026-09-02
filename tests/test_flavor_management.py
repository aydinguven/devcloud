import pytest
from sqlalchemy import update

from app.models.user import User, UserRole
from app.orchestrator.flavors import FLAVORS
from tests.conftest import TEST_WORKER_ID


async def _admin_headers(client, db_session):
    response = await client.post(
        "/api/auth/register",
        json={
            "username": "flavor-admin",
            "email": "flavor-admin@example.com",
            "password": "AdminPassword123!",
        },
    )
    user_id = response.json()["user"]["id"]
    await db_session.execute(update(User).where(User.id == user_id).values(role=UserRole.ADMIN))
    await db_session.commit()
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.mark.asyncio
async def test_admin_can_create_edit_sync_and_delete_custom_flavor(client, db_session):
    headers = await _admin_headers(client, db_session)
    flavor_id = "cpu.analysis"
    try:
        created = await client.post(
            "/api/admin/flavors",
            headers=headers,
            json={
                "id": flavor_id,
                "display_name": "Analiz",
                "description": "Yoğun analiz işleri",
                "cpus": 2.5,
                "memory_mb": 6144,
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["memory_display"] == "6 GB"

        updated = await client.patch(
            f"/api/admin/flavors/{flavor_id}",
            headers=headers,
            json={"display_name": "Büyük Analiz", "cpus": 3, "memory_mb": 8192},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["display_name"] == "Büyük Analiz"
        assert updated.json()["memory_display"] == "8 GB"

        listed = await client.get("/api/admin/flavors", headers=headers)
        assert any(item["id"] == flavor_id for item in listed.json())

        synced = await client.post("/api/admin/catalog/sync", headers=headers)
        assert synced.status_code == 200, synced.text
        assert synced.json()["total_count"] >= 1
        assert any(item["node_id"] == TEST_WORKER_ID for item in synced.json()["workers"])

        deleted = await client.delete(f"/api/admin/flavors/{flavor_id}", headers=headers)
        assert deleted.status_code == 200, deleted.text
        assert flavor_id not in FLAVORS
    finally:
        FLAVORS.pop(flavor_id, None)


@pytest.mark.asyncio
async def test_admin_can_rename_builtin_workspace_type(client, db_session):
    headers = await _admin_headers(client, db_session)
    updated = await client.patch(
        "/api/admin/templates/vscode-python",
        headers=headers,
        json={"name": "Kurumsal Python", "description": "Yönetilen Python geliştirme ortamı"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["name"] == "Kurumsal Python"

    templates = await client.get("/api/workspaces/templates", headers=headers)
    template = next(item for item in templates.json() if item["id"] == "vscode-python")
    assert template["name"] == "Kurumsal Python"
    assert template["description"] == "Yönetilen Python geliştirme ortamı"
