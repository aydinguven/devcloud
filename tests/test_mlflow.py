import pytest
from httpx import AsyncClient
from sqlalchemy import update

import app.routes.admin_routes as admin_module
from app.integrations.mlflow import MlflowClient
from app.models.user import User, UserRole
from tests.conftest import TestingSessionLocal


async def _admin_headers(client: AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/auth/register",
        json={"username": "mlflow_admin", "email": "mlflow-admin@test.com", "password": "Password123!"},
    )
    token = response.json()["access_token"]
    user_id = response.json()["user"]["id"]
    async with TestingSessionLocal() as session:
        await session.execute(update(User).where(User.id == user_id).values(role=UserRole.ADMIN))
        await session.commit()
    return {"Authorization": f"Bearer {token}"}


def _settings_payload(secret="model-registry-token"):
    return {
        "enabled": True,
        "base_url": "https://mlflow.internal",
        "auth_type": "bearer",
        "username": "",
        "secret": secret,
        "validate_tls": True,
        "ca_cert_file": "",
        "timeout_seconds": 10,
    }


@pytest.mark.asyncio
async def test_mlflow_secret_is_write_only_and_connection_can_be_tested(client: AsyncClient, monkeypatch):
    headers = await _admin_headers(client)
    saved = await client.put("/api/admin/mlflow-settings", headers=headers, json=_settings_payload())
    assert saved.status_code == 200
    assert saved.json()["has_secret"] is True
    assert "secret" not in saved.json()
    assert "model-registry-token" not in saved.text

    async def fake_test(self):
        assert self.config.secret == "model-registry-token"
        return 1, 12

    monkeypatch.setattr(admin_module.MlflowClient, "test", fake_test)
    tested = await client.post(
        "/api/admin/mlflow-settings/test",
        headers=headers,
        json=_settings_payload(secret=None),
    )
    assert tested.status_code == 200
    assert tested.json()["response_time_ms"] == 12


@pytest.mark.asyncio
async def test_authenticated_user_can_list_normalized_mlflow_models(client: AsyncClient, monkeypatch):
    headers = await _admin_headers(client)
    await client.put("/api/admin/mlflow-settings", headers=headers, json=_settings_payload())

    async def fake_search(self, search="", page_token="", max_results=100):
        assert search == "fraud"
        return {
            "registered_models": [
                {
                    "name": "fraud-detector",
                    "description": "Production classifier",
                    "aliases": ["champion"],
                    "tags": [{"key": "team", "value": "risk"}],
                    "latest_versions": [{"version": "7", "status": "READY"}],
                }
            ],
            "next_page_token": "next",
        }

    monkeypatch.setattr(MlflowClient, "search_registered_models", fake_search)
    response = await client.get("/api/mlflow/models?search=fraud", headers=headers)
    assert response.status_code == 200
    model = response.json()["models"][0]
    assert model["latest_version"]["version"] == "7"
    assert model["aliases_list"] == ["champion"]
    assert model["tags_map"] == {"team": "risk"}

