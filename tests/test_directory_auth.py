import pytest
from httpx import AsyncClient
from sqlalchemy import select, update

from app.auth.ldap import (
    DirectoryIdentity,
    decrypt_directory_secret,
    encrypt_directory_secret,
)
from app.models.directory_settings import DirectorySettings
from app.models.user import User, UserRole
from tests.conftest import TestingSessionLocal


async def _admin_headers(client: AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/auth/register",
        json={
            "username": "directory_admin",
            "email": "directory-admin@example.com",
            "password": "AdminPassword123!",
        },
    )
    user_id = response.json()["user"]["id"]
    async with TestingSessionLocal() as session:
        await session.execute(
            update(User).where(User.id == user_id).values(role=UserRole.ADMIN)
        )
        await session.commit()
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _settings_payload(**overrides):
    payload = {
        "enabled": True,
        "server_host": "ldaps.tcmb.gov.tr",
        "server_port": 686,
        "use_ssl": True,
        "validate_tls": True,
        "ca_cert_file": "",
        "connect_timeout_seconds": 10,
        "bind_dn": "CN=svc-devcloud,OU=Services,DC=example,DC=com",
        "bind_password": "bind-secret",
        "user_base_dn": "OU=Users,DC=example,DC=com",
        "user_filter": "(&(objectClass=user)(sAMAccountName={username}))",
        "username_attribute": "sAMAccountName",
        "email_attribute": "mail",
        "display_name_attribute": "displayName",
        "team_attribute": "department",
        "directorate_attribute": "division",
        "group_membership_attribute": "memberOf",
        "required_group_dn": "CN=DevCloud,OU=Groups,DC=example,DC=com",
        "admin_group_dn": "CN=DevCloud-Admins,OU=Groups,DC=example,DC=com",
        "nested_group_search": True,
    }
    payload.update(overrides)
    return payload


def test_directory_secret_encryption_round_trip():
    encrypted = encrypt_directory_secret("very-secret-password")
    assert encrypted != "very-secret-password"
    assert decrypt_directory_secret(encrypted) == "very-secret-password"


@pytest.mark.asyncio
async def test_admin_can_save_directory_settings_without_password_disclosure(
    client: AsyncClient,
):
    headers = await _admin_headers(client)
    response = await client.put(
        "/api/admin/directory-settings",
        headers=headers,
        json=_settings_payload(),
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["server_host"] == "ldaps.tcmb.gov.tr"
    assert data["server_port"] == 686
    assert data["has_bind_password"] is True
    assert data["team_attribute"] == "department"
    assert data["directorate_attribute"] == "division"
    assert "bind_password" not in data

    async with TestingSessionLocal() as session:
        record = await session.get(DirectorySettings, 1)
        assert record.encrypted_bind_password != "bind-secret"
        original_ciphertext = record.encrypted_bind_password

    payload = _settings_payload(bind_password=None, connect_timeout_seconds=15)
    second = await client.put(
        "/api/admin/directory-settings", headers=headers, json=payload
    )
    assert second.status_code == 200, second.text
    async with TestingSessionLocal() as session:
        record = await session.get(DirectorySettings, 1)
        assert record.encrypted_bind_password == original_ciphertext
        assert record.connect_timeout_seconds == 15


@pytest.mark.asyncio
async def test_directory_connection_test_uses_unsaved_form_values(
    client: AsyncClient, monkeypatch
):
    headers = await _admin_headers(client)

    def fake_test(config):
        assert config.server_host == "ldaps.tcmb.gov.tr"
        assert config.server_port == 686
        assert config.bind_password == "bind-secret"
        return "Bind ve kullanıcı tabanı araması başarılı.", 27

    monkeypatch.setattr(
        "app.routes.admin_routes.test_directory_configuration", fake_test
    )
    response = await client.post(
        "/api/admin/directory-settings/test",
        headers=headers,
        json=_settings_payload(),
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "success": True,
        "message": "Bind ve kullanıcı tabanı araması başarılı.",
        "server": "ldaps://ldaps.tcmb.gov.tr:686",
        "response_time_ms": 27,
    }


@pytest.mark.asyncio
async def test_successful_directory_login_provisions_admin_user(
    client: AsyncClient, monkeypatch
):
    async with TestingSessionLocal() as session:
        session.add(
            DirectorySettings(
                id=1,
                enabled=True,
                server_host="ldaps.tcmb.gov.tr",
                server_port=686,
                bind_dn="CN=svc,DC=example,DC=com",
                encrypted_bind_password=encrypt_directory_secret("bind-secret"),
                user_base_dn="OU=Users,DC=example,DC=com",
                required_group_dn="CN=DevCloud,DC=example,DC=com",
                admin_group_dn="CN=DevCloud-Admins,DC=example,DC=com",
            )
        )
        await session.commit()

    def fake_authenticate(config, username, password):
        assert username == "aydin"
        assert password == "user-secret"
        return DirectoryIdentity(
            username="aydin",
            email="aydin@example.com",
            full_name="Aydin Example",
            team="AI Platform",
            directorate="Data Technologies",
            user_dn="CN=Aydin,OU=Users,DC=example,DC=com",
            groups=("CN=DevCloud-Admins,DC=example,DC=com",),
            is_admin=True,
        )

    monkeypatch.setattr(
        "app.auth.ldap.authenticate_directory_user", fake_authenticate
    )
    response = await client.post(
        "/api/auth/login",
        json={"username": "aydin", "password": "user-secret"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["user"]["auth_source"] == "active_directory"
    assert response.json()["user"]["role"] == "admin"

    async with TestingSessionLocal() as session:
        user = (
            await session.execute(
                select(User).where(User.username == "aydin")
            )
        ).scalar_one()
        assert user.role == UserRole.ADMIN
        assert user.auth_source == "active_directory"
        assert user.team == "AI Platform"
        assert user.directorate == "Data Technologies"


@pytest.mark.asyncio
async def test_enabled_directory_disables_public_registration(client: AsyncClient):
    async with TestingSessionLocal() as session:
        session.add(DirectorySettings(id=1, enabled=True))
        await session.commit()

    api_response = await client.post(
        "/api/auth/register",
        json={
            "username": "bypass_user",
            "email": "bypass@example.com",
            "password": "Password123!",
        },
    )
    assert api_response.status_code == 403

    login_page = await client.get("/login")
    assert login_page.status_code == 200
    assert "Yeni Hesap Oluştur" not in login_page.text
    assert "Kurumsal dizin hesabınızla giriş yapın" in login_page.text

    register_page = await client.get("/register")
    assert register_page.status_code == 302
    assert register_page.headers["location"] == "/login"
