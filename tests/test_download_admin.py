import pytest
from httpx import AsyncClient
from sqlalchemy import update

from app.download_updates import download_update_manager
from app.models.user import User, UserRole
from tests.conftest import TestingSessionLocal


@pytest.mark.asyncio
async def test_download_update_controls_are_admin_only(
    client: AsyncClient,
    monkeypatch,
):
    regular_registration = await client.post(
        "/api/auth/register",
        json={
            "username": "download_user",
            "email": "download-user@test.com",
            "password": "Password123!",
        },
    )
    regular_headers = {
        "Authorization": f"Bearer {regular_registration.json()['access_token']}"
    }
    assert (
        await client.get("/api/admin/downloads/status", headers=regular_headers)
    ).status_code == 403
    assert (
        await client.post("/api/admin/downloads/update", headers=regular_headers)
    ).status_code == 403

    admin_registration = await client.post(
        "/api/auth/register",
        json={
            "username": "download_admin",
            "email": "download-admin@test.com",
            "password": "Password123!",
        },
    )
    admin_id = admin_registration.json()["user"]["id"]
    async with TestingSessionLocal() as session:
        await session.execute(
            update(User).where(User.id == admin_id).values(role=UserRole.ADMIN)
        )
        await session.commit()
    admin_headers = {
        "Authorization": f"Bearer {admin_registration.json()['access_token']}"
    }

    status_response = await client.get(
        "/api/admin/downloads/status", headers=admin_headers
    )
    assert status_response.status_code == 200
    assert "enabled" in status_response.json()

    monkeypatch.setattr(
        download_update_manager,
        "start",
        lambda: {"state": "queued", "enabled": True, "logs": []},
    )
    start_response = await client.post(
        "/api/admin/downloads/update", headers=admin_headers
    )
    assert start_response.status_code == 202
    assert start_response.json()["state"] == "queued"

    page = await client.get("/admin", headers=admin_headers)
    assert page.status_code == 200
    assert "Çevrim Dışı İndirmeler" in page.text
    assert 'id="btn-update-downloads"' in page.text
    assert 'id="btn-clean-downloads"' in page.text

    clean_response = await client.post(
        "/api/admin/downloads/clean", headers=admin_headers
    )
    assert clean_response.status_code == 200
    assert "cleaned_count" in clean_response.json()
    assert "freed_display" in clean_response.json()
