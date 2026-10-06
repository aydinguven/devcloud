import json

import httpx
import pytest
from sqlalchemy import select, update

import app.integrations.litellm as litellm_module
from app.models.genai_account import GenAiAccount
from app.models.genai_settings import GenAiSettings
from app.models.user import User, UserRole
from app.security.secrets import decrypt_secret

ADMIN_KEY = "sk-aifactory-admin-secret"


class FakeLiteLLM:
    """In-memory LiteLLM management API (subset used by devcloud)."""

    def __init__(self, *, user_info_404: bool = True):
        self.users: dict[str, dict] = {"aifactory": {"user_role": "proxy_admin", "spend": 0}}
        self.keys: dict[str, dict] = {}
        self.calls: list[tuple[str, str, dict]] = []
        self.user_info_404 = user_info_404
        self.fail_delete = False
        self.counter = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        path = request.url.path
        self.calls.append((request.method, path, body))
        if request.headers.get("authorization") != f"Bearer {ADMIN_KEY}":
            return httpx.Response(401, json={"error": {"message": f"Invalid key {request.headers.get('authorization')}"}})
        if path == "/user/info":
            user_id = request.url.params.get("user_id")
            if user_id not in self.users:
                if self.user_info_404:
                    return httpx.Response(404, json={"detail": f"User {user_id} not found"})
                return httpx.Response(200, json={"user_id": user_id, "user_info": None, "keys": []})
            keys = [
                {"token": token, "key_alias": key["key_alias"]}
                for token, key in self.keys.items()
                if key["user_id"] == user_id
            ]
            return httpx.Response(
                200,
                json={"user_id": user_id, "user_info": {"user_id": user_id, **self.users[user_id]}, "keys": keys},
            )
        if path == "/user/new":
            if body["user_id"] in self.users:
                return httpx.Response(400, json={"error": {"message": "User already exists"}})
            self.users[body["user_id"]] = {
                "user_role": body.get("user_role", "internal_user"),
                "spend": 1.25,
                "max_budget": body.get("max_budget"),
                "user_email": body.get("user_email"),
            }
            return httpx.Response(200, json={"user_id": body["user_id"]})
        if path == "/key/generate":
            self.counter += 1
            key = f"sk-generated-{self.counter}"
            token = f"hash-{self.counter}"
            self.keys[token] = {"user_id": body["user_id"], "key_alias": body["key_alias"]}
            return httpx.Response(200, json={"key": key, "token": token, "key_alias": body["key_alias"]})
        if path == "/key/delete":
            if self.fail_delete:
                return httpx.Response(500, json={"error": {"message": "boom"}})
            for token in body.get("keys", []):
                self.keys.pop(token, None)
            return httpx.Response(200, json={"deleted_keys": body.get("keys", [])})
        if path == "/key/info":
            return httpx.Response(200, json={"key": "hash-admin", "info": {"user_id": "aifactory"}})
        if path == "/user/daily/activity":
            return httpx.Response(
                200,
                json={"results": [{"date": "2026-10-05", "metrics": {"spend": 0.5, "total_tokens": 1200, "api_requests": 3}}]},
            )
        return httpx.Response(404, json={"detail": "unknown"})


@pytest.fixture
def fake_litellm(monkeypatch):
    fake = FakeLiteLLM()
    real_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(fake.handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(litellm_module.httpx, "AsyncClient", client_factory)
    return fake


async def _register(client, db_session, username, *, admin=False):
    response = await client.post(
        "/api/auth/register",
        json={"username": username, "email": f"{username.lower()}@example.com", "password": "Password123!x"},
    )
    assert response.status_code in {200, 201}, response.text
    payload = response.json()
    if admin:
        await db_session.execute(
            update(User).where(User.id == payload["user"]["id"]).values(role=UserRole.ADMIN)
        )
        await db_session.commit()
    return {"Authorization": f"Bearer {payload['access_token']}"}


async def _configure(client, admin_headers, **overrides):
    body = {
        "enabled": True,
        "base_url": "http://litellm.test:5003",
        "admin_key": ADMIN_KEY,
        "user_role": "internal_user_viewer",
        "models": ["gpt-4o", "claude-sonnet"],
        "max_budget": 50,
        "budget_duration": "30d",
        **overrides,
    }
    return await client.put("/api/admin/genai-settings", headers=admin_headers, json=body)


@pytest.mark.asyncio
async def test_admin_settings_store_key_encrypted_and_never_return_it(client, db_session, fake_litellm):
    admin = await _register(client, db_session, "genai-admin", admin=True)
    user = await _register(client, db_session, "genai-user")

    initial = await client.get("/api/admin/genai-settings", headers=admin)
    assert initial.status_code == 200
    assert initial.json()["managed"] is False

    missing_key = await _configure(client, admin, admin_key=None)
    assert missing_key.status_code == 422

    ui_url = await _configure(client, admin, base_url="http://litellm.test:5003/ui/")
    assert ui_url.status_code == 422

    saved = await _configure(client, admin)
    assert saved.status_code == 200, saved.text
    assert saved.json()["has_admin_key"] is True
    assert ADMIN_KEY not in saved.text
    record = await db_session.get(GenAiSettings, 1)
    assert record.encrypted_admin_key != ADMIN_KEY
    assert decrypt_secret(record.encrypted_admin_key) == ADMIN_KEY

    kept = await _configure(client, admin, admin_key=None, timeout_seconds=20)
    assert kept.status_code == 200
    await db_session.refresh(record)
    assert decrypt_secret(record.encrypted_admin_key) == ADMIN_KEY

    tested = await client.post("/api/admin/genai-settings/test", headers=admin)
    assert tested.json() == {
        "ok": True,
        "message": "LiteLLM bağlantısı ve yönetici yetkisi doğrulandı.",
        "admin_user_id": "aifactory",
        "admin_role": "proxy_admin",
        "latency_ms": tested.json()["latency_ms"],
    }
    assert ADMIN_KEY not in tested.text

    page = await client.get("/admin/integrations", headers=admin)
    assert page.status_code == 200
    assert 'id="genai-settings-form"' in page.text
    assert "gpt-4o\nclaude-sonnet" in page.text
    assert ADMIN_KEY not in page.text

    assert (await client.get("/api/admin/genai-settings", headers=user)).status_code == 403
    assert (await _configure(client, user)).status_code == 403


@pytest.mark.asyncio
async def test_unconfigured_genai_reports_state_and_refuses_provisioning(client, db_session, fake_litellm):
    user = await _register(client, db_session, "k015570")

    status = await client.get("/api/genai/account", headers=user)
    assert status.status_code == 200
    assert status.json()["configured"] is False

    created = await client.post("/api/genai/account", headers=user)
    assert created.status_code == 503
    assert fake_litellm.calls == []

    page = await client.get("/genai", headers=user)
    assert page.status_code == 200
    assert "Henüz yapılandırılmadı" in page.text


@pytest.mark.asyncio
async def test_user_provisions_litellm_user_and_sees_key_once(client, db_session, fake_litellm):
    admin = await _register(client, db_session, "genai-admin", admin=True)
    assert (await _configure(client, admin)).status_code == 200
    user = await _register(client, db_session, "K015570")

    before = (await client.get("/api/genai/account", headers=user)).json()
    assert before["configured"] is True
    assert before["provisioned"] is False
    assert before["litellm_user_exists"] is False
    assert before["litellm_user_id"] == "k015570"

    created = await client.post("/api/genai/account", headers=user)
    assert created.status_code == 200, created.text
    assert created.headers["cache-control"] == "no-store"
    issued = created.json()
    assert issued["api_key"] == "sk-generated-1"
    assert issued["litellm_user_id"] == "k015570"
    assert issued["base_url"] == "http://litellm.test:5003"
    assert issued["key_alias"].startswith("devcloud-k015570-")

    new_user = next(body for method, path, body in fake_litellm.calls if path == "/user/new")
    assert new_user == {
        "user_id": "k015570",
        "auto_create_key": False,
        "metadata": {"source": "devcloud"},
        "user_email": "k015570@example.com",
        "user_role": "internal_user_viewer",
        "max_budget": 50.0,
        "budget_duration": "30d",
    }
    key_request = next(body for method, path, body in fake_litellm.calls if path == "/key/generate")
    assert key_request["user_id"] == "k015570"
    assert key_request["models"] == ["gpt-4o", "claude-sonnet"]
    assert "max_budget" not in key_request

    account = (await db_session.execute(select(GenAiAccount))).scalar_one()
    assert account.personal_key_token == "hash-1"
    columns = {column.name: getattr(account, column.name) for column in GenAiAccount.__table__.columns}
    assert "sk-generated-1" not in json.dumps(columns, default=str)

    after = await client.get("/api/genai/account", headers=user)
    body = after.json()
    assert body["provisioned"] is True
    assert body["key_active"] is True
    assert body["usage"]["spend"] == 1.25
    assert body["usage"]["max_budget"] == 50.0
    assert "sk-generated-1" not in after.text

    again = await client.post("/api/genai/account", headers=user)
    assert again.status_code == 409
    assert len(fake_litellm.keys) == 1

    history = (await client.get("/api/genai/usage", headers=user)).json()
    assert history == {
        "available": True,
        "days": [{"date": "2026-10-05", "spend": 0.5, "total_tokens": 1200, "api_requests": 3}],
    }

    page = await client.get("/genai", headers=user)
    assert page.status_code == 200
    assert "/static/js/genai.js" in page.text


@pytest.mark.asyncio
async def test_existing_litellm_user_is_adopted(client, db_session, fake_litellm):
    fake_litellm.user_info_404 = False  # older LiteLLM: 200 with empty user_info
    fake_litellm.users["k099999"] = {"user_role": "internal_user", "spend": 7.5}
    admin = await _register(client, db_session, "genai-admin", admin=True)
    assert (await _configure(client, admin)).status_code == 200
    user = await _register(client, db_session, "k099999")

    status = (await client.get("/api/genai/account", headers=user)).json()
    assert status["provisioned"] is False
    assert status["litellm_user_exists"] is True

    created = await client.post("/api/genai/account", headers=user)
    assert created.status_code == 200, created.text
    assert not any(path == "/user/new" for _method, path, _body in fake_litellm.calls)
    assert fake_litellm.users["k099999"]["spend"] == 7.5


@pytest.mark.asyncio
async def test_rotation_replaces_key_and_deletes_the_old_one(client, db_session, fake_litellm):
    admin = await _register(client, db_session, "genai-admin", admin=True)
    assert (await _configure(client, admin)).status_code == 200
    user = await _register(client, db_session, "k015570")

    assert (await client.post("/api/genai/account/rotate", headers=user)).status_code == 409
    first = (await client.post("/api/genai/account", headers=user)).json()

    rotated = await client.post("/api/genai/account/rotate", headers=user)
    assert rotated.status_code == 200, rotated.text
    second = rotated.json()
    assert second["api_key"] != first["api_key"]
    assert second["warning"] is None
    assert set(fake_litellm.keys) == {"hash-2"}
    account = (await db_session.execute(select(GenAiAccount))).scalar_one()
    assert account.personal_key_token == "hash-2"
    assert account.rotated_at is not None

    fake_litellm.fail_delete = True
    third = (await client.post("/api/genai/account/rotate", headers=user)).json()
    assert third["warning"]
    await db_session.refresh(account)
    assert account.personal_key_token == "hash-3"


@pytest.mark.asyncio
async def test_litellm_errors_never_leak_secrets(client, db_session, fake_litellm):
    admin = await _register(client, db_session, "genai-admin", admin=True)
    assert (await _configure(client, admin, admin_key="sk-wrong-admin-key")).status_code == 200
    user = await _register(client, db_session, "k015570")

    status = await client.get("/api/genai/account", headers=user)
    assert status.status_code == 200
    assert "401" in status.json()["error"]
    assert "sk-wrong-admin-key" not in status.text

    created = await client.post("/api/genai/account", headers=user)
    assert created.status_code == 502
    assert "sk-wrong-admin-key" not in created.text

    tested = await client.post("/api/admin/genai-settings/test", headers=admin)
    assert tested.json()["ok"] is False
    assert "sk-wrong-admin-key" not in tested.text
