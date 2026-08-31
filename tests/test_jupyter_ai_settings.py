import hashlib
import json

import httpx
import pytest
from sqlalchemy import update

import app.worker_agent as worker_module
from app.config import settings
from app.jupyter_ai import default_model_catalog
from app.models.jupyter_ai_settings import JupyterAiSettings
from app.models.node import Node
from app.models.user import User, UserRole
from app.security.secrets import decrypt_secret
from app.worker_agent import WorkerAgent
from tests.conftest import TEST_WORKER_ID


@pytest.mark.asyncio
async def test_admin_encrypts_settings_and_worker_receives_shared_token(
    client, db_session
):
    registration = await client.post(
        "/api/auth/register",
        json={
            "username": "jupyter-ai-admin",
            "email": "jupyter-ai-admin@example.com",
            "password": "AdminPassword123!",
        },
    )
    admin_id = registration.json()["user"]["id"]
    await db_session.execute(
        update(User).where(User.id == admin_id).values(role=UserRole.ADMIN)
    )
    await db_session.commit()
    admin_headers = {
        "Authorization": f"Bearer {registration.json()['access_token']}"
    }

    initial = await client.get(
        "/api/admin/jupyter-ai-settings", headers=admin_headers
    )
    assert initial.status_code == 200
    assert initial.json() == {
        "managed": False,
        "enabled": False,
        "gateway_url": "",
        "model_id": "",
        "gateway_model_discovery": False,
        "models": default_model_catalog(),
        "has_shared_token": False,
        "updated_at": None,
    }

    missing_token = await client.put(
        "/api/admin/jupyter-ai-settings",
        headers=admin_headers,
        json={
            "enabled": True,
            "gateway_url": "https://llm.internal.example",
            "model_id": "qwen3.6-35b",
            "shared_token": None,
        },
    )
    assert missing_token.status_code == 422

    shared_token = "shared-jupyter-ai-token"
    saved = await client.put(
        "/api/admin/jupyter-ai-settings",
        headers=admin_headers,
        json={
            "enabled": True,
            "gateway_url": "https://llm.internal.example/",
            "model_id": "qwen3.6-35b",
            "gateway_model_discovery": True,
            "models": default_model_catalog(),
            "shared_token": shared_token,
        },
    )
    assert saved.status_code == 200
    assert saved.json()["gateway_url"] == "https://llm.internal.example"
    assert saved.json()["has_shared_token"] is True
    assert saved.json()["gateway_model_discovery"] is True
    assert len(saved.json()["models"]) == 5
    assert shared_token not in json.dumps(saved.json())

    record = await db_session.get(JupyterAiSettings, 1)
    assert record is not None
    assert record.encrypted_shared_token != shared_token
    assert decrypt_secret(record.encrypted_shared_token) == shared_token

    worker_token = "worker-jupyter-ai-token"
    worker = await db_session.get(Node, TEST_WORKER_ID)
    worker.agent_token_hash = hashlib.sha256(worker_token.encode()).hexdigest()
    db_session.add(worker)
    await db_session.commit()

    worker_response = await client.get(
        "/api/agent/jupyter-ai-settings",
        params={"node_id": TEST_WORKER_ID},
        headers={"Authorization": f"Bearer {worker_token}"},
    )
    assert worker_response.status_code == 200
    assert worker_response.headers["cache-control"] == "no-store"
    assert worker_response.json()["managed"] is True
    assert worker_response.json()["shared_token"] == shared_token
    assert worker_response.json()["gateway_model_discovery"] is True
    assert worker_response.json()["models"][2]["model_id"] == (
        "openrouter/deepseek/deepseek-v4-pro"
    )

    forbidden = await client.get(
        "/api/agent/jupyter-ai-settings",
        params={"node_id": TEST_WORKER_ID},
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert forbidden.status_code == 403

    preserved = await client.put(
        "/api/admin/jupyter-ai-settings",
        headers=admin_headers,
        json={
            "enabled": True,
            "gateway_url": "https://llm.internal.example",
            "model_id": "qwen3.6-35b",
            "gateway_model_discovery": True,
            "models": None,
            "shared_token": None,
        },
    )
    assert preserved.status_code == 200
    await db_session.refresh(record)
    assert decrypt_secret(record.encrypted_shared_token) == shared_token

    cleared = await client.put(
        "/api/admin/jupyter-ai-settings",
        headers=admin_headers,
        json={
            "enabled": False,
            "gateway_url": "",
            "model_id": "",
            "gateway_model_discovery": False,
            "models": [],
            "shared_token": "",
        },
    )
    assert cleared.status_code == 200
    assert cleared.json()["has_shared_token"] is False
    await db_session.refresh(record)
    assert record.encrypted_shared_token == ""


@pytest.mark.asyncio
async def test_worker_applies_central_settings_and_preserves_unmanaged_fallback(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "STORAGE_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "JUPYTER_AI_GATEWAY_URL", "http://legacy")
    monkeypatch.setattr(settings, "JUPYTER_AI_MODEL", "legacy-model")
    monkeypatch.setattr(settings, "JUPYTER_AI_GATEWAY_TOKEN", "legacy-token")
    monkeypatch.setattr(settings, "JUPYTER_AI_GATEWAY_MODEL_DISCOVERY", False)
    monkeypatch.setattr(settings, "JUPYTER_AI_MODEL_CATALOG_JSON", "[]")
    monkeypatch.setenv("DEVCLOUD_CONTROLLER_URL", "https://controller.example")
    monkeypatch.setenv("DEVCLOUD_NODE_ID", "worker-1")
    monkeypatch.setenv("DEVCLOUD_NODE_TOKEN", "worker-token")
    payload = {"managed": False, "enabled": False}

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, *_args, **_kwargs):
            return httpx.Response(
                200,
                json=payload,
                request=httpx.Request(
                    "GET", "https://controller.example/api/agent/jupyter-ai-settings"
                ),
            )

    monkeypatch.setattr(worker_module.httpx, "AsyncClient", FakeClient)
    agent = WorkerAgent()

    assert await agent.sync_jupyter_ai_settings() is False
    assert settings.JUPYTER_AI_GATEWAY_URL == "http://legacy"
    assert settings.JUPYTER_AI_MODEL == "legacy-model"
    assert settings.JUPYTER_AI_GATEWAY_TOKEN == "legacy-token"

    payload.update(
        {
            "managed": True,
            "enabled": True,
            "gateway_url": "https://gateway.internal.example/",
            "model_id": "qwen3.6-35b",
            "shared_token": "central-token",
            "gateway_model_discovery": True,
            "models": default_model_catalog(),
        }
    )
    assert await agent.sync_jupyter_ai_settings() is True
    assert settings.JUPYTER_AI_GATEWAY_URL == "https://gateway.internal.example"
    assert settings.JUPYTER_AI_MODEL == "qwen3.6-35b"
    assert settings.JUPYTER_AI_GATEWAY_TOKEN == "central-token"
    assert settings.JUPYTER_AI_GATEWAY_MODEL_DISCOVERY is True
    assert json.loads(settings.JUPYTER_AI_MODEL_CATALOG_JSON) == (
        default_model_catalog()
    )

    payload.clear()
    payload.update({"managed": True, "enabled": False})
    assert await agent.sync_jupyter_ai_settings() is True
    assert settings.JUPYTER_AI_GATEWAY_URL == ""
    assert settings.JUPYTER_AI_MODEL == ""
    assert settings.JUPYTER_AI_GATEWAY_TOKEN == ""
    assert settings.JUPYTER_AI_GATEWAY_MODEL_DISCOVERY is False
    assert settings.JUPYTER_AI_MODEL_CATALOG_JSON == "[]"


@pytest.mark.asyncio
async def test_admin_rejects_unsafe_jupyter_ai_gateway(client, db_session):
    registration = await client.post(
        "/api/auth/register",
        json={
            "username": "jupyter-ai-validator",
            "email": "jupyter-ai-validator@example.com",
            "password": "AdminPassword123!",
        },
    )
    admin_id = registration.json()["user"]["id"]
    await db_session.execute(
        update(User).where(User.id == admin_id).values(role=UserRole.ADMIN)
    )
    await db_session.commit()
    response = await client.put(
        "/api/admin/jupyter-ai-settings",
        headers={"Authorization": f"Bearer {registration.json()['access_token']}"},
        json={
            "enabled": True,
            "gateway_url": "https://user:secret@llm.example?token=leak",
            "model_id": "model id",
            "gateway_model_discovery": True,
            "models": [
                {"model_id": "model id", "name": "Unsafe", "description": ""}
            ],
            "shared_token": "token",
        },
    )
    assert response.status_code == 422
    assert await db_session.get(JupyterAiSettings, 1) is None


@pytest.mark.asyncio
async def test_admin_rejects_duplicate_jupyter_ai_models(client, db_session):
    registration = await client.post(
        "/api/auth/register",
        json={
            "username": "jupyter-ai-duplicates",
            "email": "jupyter-ai-duplicates@example.com",
            "password": "AdminPassword123!",
        },
    )
    admin_id = registration.json()["user"]["id"]
    await db_session.execute(
        update(User).where(User.id == admin_id).values(role=UserRole.ADMIN)
    )
    await db_session.commit()
    model = {"model_id": "claude-internal", "name": "Internal", "description": ""}
    response = await client.put(
        "/api/admin/jupyter-ai-settings",
        headers={"Authorization": f"Bearer {registration.json()['access_token']}"},
        json={
            "enabled": True,
            "gateway_url": "https://llm.example",
            "model_id": "claude-internal",
            "gateway_model_discovery": True,
            "models": [model, model],
            "shared_token": "token",
        },
    )
    assert response.status_code == 422
