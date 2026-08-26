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
    assert (
        await client.get("/api/admin/downloads/worker/status", headers=regular_headers)
    ).status_code == 403
    assert (
        await client.post("/api/admin/downloads/worker/update", headers=regular_headers)
    ).status_code == 403
    assert (
        await client.get("/api/admin/download-settings", headers=regular_headers)
    ).status_code == 403
    assert (
        await client.put(
            "/api/admin/download-settings",
            headers=regular_headers,
            json={"public_base_url": "https://forbidden.example.com"},
        )
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

    download_settings = await client.get(
        "/api/admin/download-settings", headers=admin_headers
    )
    assert download_settings.status_code == 200
    assert download_settings.json()["public_base_url"] == "http://10.253.6.189"

    saved_settings = await client.put(
        "/api/admin/download-settings",
        headers=admin_headers,
        json={"public_base_url": "https://master.internal.example/"},
    )
    assert saved_settings.status_code == 200
    assert saved_settings.json() == {
        "public_base_url": "https://master.internal.example",
        "worker_bootstrap_url": (
            "https://master.internal.example/download/install-worker.sh"
        ),
    }
    invalid_settings = await client.put(
        "/api/admin/download-settings",
        headers=admin_headers,
        json={"public_base_url": "file:///etc/passwd"},
    )
    assert invalid_settings.status_code == 422
    command_injection = await client.put(
        "/api/admin/download-settings",
        headers=admin_headers,
        json={"public_base_url": "https://master.example.com/;touch-pwned"},
    )
    assert command_injection.status_code == 422

    status_response = await client.get(
        "/api/admin/downloads/status", headers=admin_headers
    )
    assert status_response.status_code == 200
    assert "enabled" in status_response.json()

    monkeypatch.setattr(
        download_update_manager,
        "start",
        lambda bundle_role="server": {
            "state": "queued",
            "enabled": True,
            "logs": [],
            "bundle_role": bundle_role,
        },
    )
    start_response = await client.post(
        "/api/admin/downloads/update", headers=admin_headers
    )
    assert start_response.status_code == 202
    assert start_response.json()["state"] == "queued"

    worker_status = await client.get(
        "/api/admin/downloads/worker/status", headers=admin_headers
    )
    assert worker_status.status_code == 200
    assert worker_status.json()["bundle_role"] == "worker"
    worker_start = await client.post(
        "/api/admin/downloads/worker/update", headers=admin_headers
    )
    assert worker_start.status_code == 202
    assert worker_start.json()["bundle_role"] == "worker"

    page = await client.get("/admin", headers=admin_headers)
    assert page.status_code == 200
    assert "Çevrim Dışı İndirmeler" in page.text
    assert 'id="btn-update-downloads"' in page.text
    assert 'id="btn-update-worker-downloads"' in page.text
    assert 'id="btn-clean-downloads"' in page.text
    assert 'id="download-settings-form"' in page.text
    assert 'value="https://master.internal.example"' in page.text

    clean_response = await client.post(
        "/api/admin/downloads/clean", headers=admin_headers
    )
    assert clean_response.status_code == 200
    assert "cleaned_count" in clean_response.json()
    assert "freed_display" in clean_response.json()
