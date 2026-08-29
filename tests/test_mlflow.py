import pytest
from httpx import AsyncClient
from sqlalchemy import select

import app.routes.mlflow_routes as mlflow_module
from app.integrations.mlflow import MlflowClient
from app.models.mlflow_settings import MlflowSettings
from tests.conftest import TestingSessionLocal


async def _user_headers(
    client: AsyncClient,
    username: str,
) -> tuple[dict[str, str], int]:
    response = await client.post(
        "/api/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "Password123!",
        },
    )
    return (
        {"Authorization": f"Bearer {response.json()['access_token']}"},
        response.json()["user"]["id"],
    )


def _settings_payload(
    secret: str | None = "model-registry-token",
    base_url: str = "https://mlflow.internal",
):
    return {
        "enabled": True,
        "base_url": base_url,
        "auth_type": "bearer",
        "username": "",
        "secret": secret,
        "validate_tls": True,
        "ca_cert_file": "",
        "timeout_seconds": 10,
    }


@pytest.mark.asyncio
async def test_user_mlflow_secret_is_write_only_and_connection_can_be_tested(
    client: AsyncClient,
    monkeypatch,
):
    headers, user_id = await _user_headers(client, "mlflow_user")
    saved = await client.put(
        "/api/mlflow/settings",
        headers=headers,
        json=_settings_payload(),
    )
    assert saved.status_code == 200
    assert saved.json()["has_secret"] is True
    assert "secret" not in saved.json()
    assert "model-registry-token" not in saved.text

    async with TestingSessionLocal() as session:
        record = (
            await session.execute(
                select(MlflowSettings).where(MlflowSettings.user_id == user_id)
            )
        ).scalar_one()
        assert record.encrypted_secret
        assert record.encrypted_secret != "model-registry-token"

    async def fake_test(self):
        assert self.config.secret == "model-registry-token"
        return 1, 12

    monkeypatch.setattr(mlflow_module.MlflowClient, "test", fake_test)
    tested = await client.post(
        "/api/mlflow/settings/test",
        headers=headers,
        json=_settings_payload(secret=None),
    )
    assert tested.status_code == 200
    assert tested.json()["response_time_ms"] == 12


@pytest.mark.asyncio
async def test_mlflow_settings_and_models_are_isolated_per_user(
    client: AsyncClient,
    monkeypatch,
):
    alice_headers, _ = await _user_headers(client, "mlflow_alice")
    bob_headers, _ = await _user_headers(client, "mlflow_bob")
    await client.put(
        "/api/mlflow/settings",
        headers=alice_headers,
        json=_settings_payload("alice-token", "https://alice-mlflow.internal"),
    )
    await client.put(
        "/api/mlflow/settings",
        headers=bob_headers,
        json=_settings_payload("bob-token", "https://bob-mlflow.internal"),
    )

    async def fake_search(self, search="", page_token="", max_results=100):
        owner = "alice" if "alice-mlflow" in self.config.base_url else "bob"
        assert self.config.secret == f"{owner}-token"
        return {
            "registered_models": [
                {
                    "name": f"{owner}-model",
                    "description": "User-owned registry model",
                    "aliases": ["champion"],
                    "tags": [{"key": "owner", "value": owner}],
                    "latest_versions": [{"version": "7", "status": "READY"}],
                }
            ],
            "next_page_token": "",
        }

    monkeypatch.setattr(MlflowClient, "search_registered_models", fake_search)
    alice_models = await client.get("/api/mlflow/models", headers=alice_headers)
    bob_models = await client.get("/api/mlflow/models", headers=bob_headers)

    assert alice_models.status_code == 200
    assert bob_models.status_code == 200
    assert alice_models.json()["models"][0]["name"] == "alice-model"
    assert bob_models.json()["models"][0]["name"] == "bob-model"
    assert alice_models.json()["models"][0]["tags_map"] == {"owner": "alice"}
    assert bob_models.json()["models"][0]["tags_map"] == {"owner": "bob"}

    alice_settings = await client.get("/api/mlflow/settings", headers=alice_headers)
    bob_settings = await client.get("/api/mlflow/settings", headers=bob_headers)
    assert alice_settings.json()["base_url"] == "https://alice-mlflow.internal"
    assert bob_settings.json()["base_url"] == "https://bob-mlflow.internal"


@pytest.mark.asyncio
async def test_user_without_mlflow_settings_gets_setup_guidance(client: AsyncClient):
    headers, _ = await _user_headers(client, "mlflow_unconfigured")
    settings = await client.get("/api/mlflow/settings", headers=headers)
    models = await client.get("/api/mlflow/models", headers=headers)

    assert settings.status_code == 200
    assert settings.json()["enabled"] is False
    assert settings.json()["has_secret"] is False
    assert models.status_code == 503
    assert "ML Modelleri" in models.json()["detail"]
