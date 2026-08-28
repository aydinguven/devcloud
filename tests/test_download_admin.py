import pytest
from httpx import AsyncClient
from sqlalchemy import update

from app.config import settings
from app.download_updates import download_update_manager
from app.ingress_settings import CertificateInfo, ingress_manager
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
    assert (
        await client.post(
            "/api/admin/download-settings/https",
            headers=regular_headers,
            data={
                "https_enabled": "false",
                "https_hostname": "aifactory.tcmb.gov.tr",
                "http_fallback_enabled": "true",
            },
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
        "https_enabled": False,
        "https_hostname": settings.HTTPS_DEFAULT_HOSTNAME,
        "http_fallback_enabled": True,
        "certificate_uploaded": False,
        "certificate_subject": None,
        "certificate_not_after": None,
        "certificate_sha256": None,
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

    async def fake_apply(**kwargs):
        assert kwargs["https_enabled"] is True
        assert kwargs["hostname"] == "aifactory.tcmb.gov.tr"
        assert kwargs["http_fallback_enabled"] is True
        assert kwargs["certificate_pem"] == b"test-certificate"
        assert kwargs["private_key_pem"] == b"test-private-key"
        return CertificateInfo(
            subject="CN=aifactory.tcmb.gov.tr",
            not_after="2027-08-26T00:00:00+00:00",
            sha256="a" * 64,
        )

    monkeypatch.setattr(ingress_manager, "apply", fake_apply)
    https_response = await client.post(
        "/api/admin/download-settings/https",
        headers=admin_headers,
        data={
            "https_enabled": "true",
            "https_hostname": "AIFACTORY.TCMB.GOV.TR.",
            "http_fallback_enabled": "true",
        },
        files={
            "certificate": ("certificate.pem", b"test-certificate", "application/x-pem-file"),
            "private_key": ("private-key.pem", b"test-private-key", "application/x-pem-file"),
        },
    )
    assert https_response.status_code == 200, https_response.text
    https_data = https_response.json()
    assert https_data["https_enabled"] is True
    assert https_data["http_fallback_enabled"] is True
    assert https_data["public_base_url"] == "https://aifactory.tcmb.gov.tr"
    assert https_data["certificate_uploaded"] is True
    assert https_data["certificate_sha256"] == "a" * 64
    assert "test-private-key" not in https_response.text

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

    page = await client.get("/admin/system", headers=admin_headers)
    assert page.status_code == 200
    assert "Çevrim Dışı İndirmeler" in page.text
    assert 'id="btn-update-downloads"' in page.text
    assert 'id="btn-update-worker-downloads"' in page.text
    assert 'id="btn-clean-downloads"' in page.text
    assert 'id="download-settings-form"' in page.text
    assert 'id="https-settings-form"' in page.text
    assert 'name="certificate"' in page.text
    assert 'name="private_key"' in page.text
    assert 'value="https://aifactory.tcmb.gov.tr"' in page.text

    clean_response = await client.post(
        "/api/admin/downloads/clean", headers=admin_headers
    )
    assert clean_response.status_code == 200
    assert "cleaned_count" in clean_response.json()
    assert "freed_display" in clean_response.json()
