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
        self.unreachable = False
        self.counter = 0
        self.teams = {
            "team-std": {"team_alias": "TCMB_Standard_User", "members": []},
            "team-ai": {"team_alias": "TCMB_AI_User", "members": []},
            "team-pro": {"team_alias": "TCMB_Pro_User", "members": []},
        }

    def member_teams(self, user_id):
        return [tid for tid, team in self.teams.items() if user_id in team["members"]]

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.unreachable:
            raise httpx.ConnectError("connection refused", request=request)
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
            team_ids = self.member_teams(user_id)
            return httpx.Response(
                200,
                json={
                    "user_id": user_id,
                    "user_info": {"user_id": user_id, **self.users[user_id], "teams": team_ids},
                    "keys": keys,
                    "teams": [{"team_id": tid, "team_alias": self.teams[tid]["team_alias"]} for tid in team_ids],
                },
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
            self.keys[token] = {
                "user_id": body["user_id"],
                "key_alias": body["key_alias"],
                "team_id": body.get("team_id"),
                "key": key,
                "purpose": body["metadata"]["purpose"],
            }
            return httpx.Response(200, json={"key": key, "token": token, "key_alias": body["key_alias"]})
        if path == "/key/delete":
            if self.fail_delete:
                return httpx.Response(500, json={"error": {"message": "boom"}})
            for token in body.get("keys", []):
                self.keys.pop(token, None)
            return httpx.Response(200, json={"deleted_keys": body.get("keys", [])})
        if path == "/team/list":
            return httpx.Response(
                200,
                json=[{"team_id": tid, "team_alias": team["team_alias"]} for tid, team in self.teams.items()],
            )
        if path == "/team/member_add":
            team = self.teams.get(body["team_id"])
            if team is None:
                return httpx.Response(404, json={"detail": "team not found"})
            user_id = body["member"]["user_id"]
            if user_id in team["members"]:
                return httpx.Response(400, json={"error": {"message": "User already in team"}})
            team["members"].append(user_id)
            return httpx.Response(200, json={"team_id": body["team_id"]})
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


TEAM_SETTINGS = {
    "default_team": "TCMB_Standard_User",
    "team_priority": ["TCMB_Pro_User", "TCMB_AI_User", "TCMB_Standard_User"],
}


@pytest.mark.asyncio
async def test_new_users_join_default_team_and_keys_bind_to_highest_tier(
    client, db_session, fake_litellm
):
    admin = await _register(client, db_session, "genai-admin", admin=True)
    assert (await _configure(client, admin, **TEAM_SETTINGS)).status_code == 200
    tested = (await client.post("/api/admin/genai-settings/test", headers=admin)).json()
    assert tested["ok"] is True
    assert "takımlar" in tested["message"]

    user = await _register(client, db_session, "k015570")
    created = await client.post("/api/genai/account", headers=user)
    assert created.status_code == 200, created.text
    assert created.json()["team"] == "TCMB_Standard_User"
    assert fake_litellm.teams["team-std"]["members"] == ["k015570"]
    assert fake_litellm.keys["hash-1"]["team_id"] == "team-std"

    status = (await client.get("/api/genai/account", headers=user)).json()
    assert status["team"] == "TCMB_Standard_User"
    assert status["key_team_current"] is True

    # An admin upgrades the user in LiteLLM; the page asks for a new key.
    fake_litellm.teams["team-pro"]["members"].append("k015570")
    status = (await client.get("/api/genai/account", headers=user)).json()
    assert status["team"] == "TCMB_Pro_User"
    assert status["key_team"] == "TCMB_Standard_User"
    assert status["key_team_current"] is False

    rotated = (await client.post("/api/genai/account/rotate", headers=user)).json()
    assert rotated["team"] == "TCMB_Pro_User"
    assert fake_litellm.keys["hash-2"]["team_id"] == "team-pro"
    # Still a Standard member: rotation never removes memberships.
    assert "k015570" in fake_litellm.teams["team-std"]["members"]


@pytest.mark.asyncio
async def test_adopted_user_keeps_existing_team(client, db_session, fake_litellm):
    fake_litellm.users["k077777"] = {"user_role": "internal_user", "spend": 0}
    fake_litellm.teams["team-ai"]["members"].append("k077777")
    admin = await _register(client, db_session, "genai-admin", admin=True)
    assert (await _configure(client, admin, **TEAM_SETTINGS)).status_code == 200
    user = await _register(client, db_session, "k077777")

    created = (await client.post("/api/genai/account", headers=user)).json()
    assert created["team"] == "TCMB_AI_User"
    assert fake_litellm.teams["team-std"]["members"] == []


@pytest.mark.asyncio
async def test_missing_default_team_is_reported(client, db_session, fake_litellm):
    admin = await _register(client, db_session, "genai-admin", admin=True)
    assert (
        await _configure(client, admin, default_team="TCMB_Typo_User")
    ).status_code == 200
    tested = (await client.post("/api/admin/genai-settings/test", headers=admin)).json()
    assert tested["ok"] is False
    assert "TCMB_Typo_User" in tested["message"]

    user = await _register(client, db_session, "k015570")
    created = await client.post("/api/genai/account", headers=user)
    assert created.status_code == 409
    assert "TCMB_Typo_User" in created.json()["detail"]
    assert fake_litellm.keys == {}


async def _enable_workspace_ai(db_session, gateway_url="http://litellm.test:5003"):
    from app.models.jupyter_ai_settings import JupyterAiSettings

    db_session.add(
        JupyterAiSettings(id=1, enabled=True, gateway_url=gateway_url, model_id="m")
    )
    await db_session.commit()


@pytest.mark.asyncio
async def test_workspace_key_is_per_user_reused_and_follows_team(
    client, db_session, fake_litellm
):
    from app.genai import workspace_gateway_token

    admin = await _register(client, db_session, "genai-admin", admin=True)
    assert (await _configure(client, admin, **TEAM_SETTINGS)).status_code == 200
    await _register(client, db_session, "k015570")
    user_row = (
        await db_session.execute(select(User).where(User.username == "k015570"))
    ).scalar_one()

    # Not opted in on the GenAI tab yet: shared key.
    await _enable_workspace_ai(db_session)
    assert await workspace_gateway_token(db_session, user_row.id) == ""
    assert fake_litellm.keys == {}

    headers = {"Authorization": f"Bearer {await _token(client, 'k015570')}"}
    assert (await client.post("/api/genai/account", headers=headers)).status_code == 200

    first = await workspace_gateway_token(db_session, user_row.id)
    assert first.startswith("sk-generated-")
    workspace_keys = {t: k for t, k in fake_litellm.keys.items() if k["purpose"] == "workspace"}
    assert len(workspace_keys) == 1
    (token, key), = workspace_keys.items()
    assert key["team_id"] == "team-std"
    assert key["key_alias"].endswith("-workspace")
    account = (await db_session.execute(select(GenAiAccount))).scalar_one()
    assert first not in json.dumps(
        {c.name: getattr(account, c.name) for c in GenAiAccount.__table__.columns},
        default=str,
    )

    assert await workspace_gateway_token(db_session, user_row.id) == first
    assert len([k for k in fake_litellm.keys.values() if k["purpose"] == "workspace"]) == 1

    # Tier upgrade: next workspace gets a Pro key; the Standard one is deleted.
    fake_litellm.teams["team-pro"]["members"].append("k015570")
    second = await workspace_gateway_token(db_session, user_row.id)
    assert second != first
    workspace_keys = [k for k in fake_litellm.keys.values() if k["purpose"] == "workspace"]
    assert [k["team_id"] for k in workspace_keys] == ["team-pro"]
    assert token not in fake_litellm.keys

    # LiteLLM down: keep the stored key instead of falling back.
    fake_litellm.unreachable = True
    assert await workspace_gateway_token(db_session, user_row.id) == second

    status = (await client.get("/api/genai/account", headers=headers)).json()
    assert status["workspace_key"] is True
    assert second not in json.dumps(status)


@pytest.mark.asyncio
async def test_workspace_key_falls_back_to_shared_key(client, db_session, fake_litellm):
    from app.genai import workspace_gateway_token

    admin = await _register(client, db_session, "genai-admin", admin=True)
    assert (await _configure(client, admin)).status_code == 200
    headers = await _register(client, db_session, "k015570")
    assert (await client.post("/api/genai/account", headers=headers)).status_code == 200
    user_row = (
        await db_session.execute(select(User).where(User.username == "k015570"))
    ).scalar_one()

    # Workspace AI disabled.
    assert await workspace_gateway_token(db_session, user_row.id) == ""
    # Workspace AI points at a different gateway than GenAI.
    await _enable_workspace_ai(db_session, gateway_url="http://other-gateway:4000")
    assert await workspace_gateway_token(db_session, user_row.id) == ""
    # Same gateway but LiteLLM unreachable and no stored key yet.
    from app.models.jupyter_ai_settings import JupyterAiSettings

    record = await db_session.get(JupyterAiSettings, 1)
    record.gateway_url = "http://LITELLM.test:5003/"
    await db_session.commit()
    fake_litellm.unreachable = True
    assert await workspace_gateway_token(db_session, user_row.id) == ""
    fake_litellm.unreachable = False
    assert (await workspace_gateway_token(db_session, user_row.id)).startswith("sk-")


async def _token(client, username):
    response = await client.post(
        "/api/auth/login", json={"username": username, "password": "Password123!x"}
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


@pytest.mark.asyncio
async def test_container_uses_per_user_gateway_token(monkeypatch):
    import asyncio

    from app.config import settings
    from app.orchestrator.podman_service import PodmanService

    svc = PodmanService(podman_bin="podman")
    svc._mock_mode = False
    commands = []

    async def fake_run_cmd(*args, **kwargs):
        commands.append(args)
        return 0, "container-id", ""

    async def fake_ensure_image_exists(*args, **kwargs):
        return True

    class FakeWriter:
        def close(self):
            return None

        async def wait_closed(self):
            return None

    async def fake_open_connection(*args, **kwargs):
        return object(), FakeWriter()

    monkeypatch.setattr(svc, "ensure_workspace_storage", lambda user_id, workspace_id: "/workspace")
    monkeypatch.setattr(svc, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(svc, "ensure_image_exists", fake_ensure_image_exists)
    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)
    monkeypatch.setattr(settings, "JUPYTER_AI_GATEWAY_URL", "https://llm-gateway.internal")
    monkeypatch.setattr(settings, "JUPYTER_AI_MODEL", "local-coder")
    monkeypatch.setattr(settings, "JUPYTER_AI_GATEWAY_TOKEN", "shared-ai-token")

    for template_id, name in (("jupyter-python", "devcloud-1-aaaaaaaa"), ("vscode-python", "devcloud-1-bbbbbbbb")):
        await svc.create_workspace_container(
            workspace_id="12345678-1234-1234-1234-123456789abc",
            user_id=1,
            container_name=name,
            template_id=template_id,
            flavor_id="t1.micro",
            host_port=10100,
            workspace_token="secret-workspace-token",
            ai_gateway_token="sk-user-own-key",
        )
    runs = [args for args in commands if args[0] == "run"]
    jupyter, vscode = runs
    assert "ANTHROPIC_AUTH_TOKEN=sk-user-own-key" in jupyter
    assert not any("shared-ai-token" in value for value in jupyter)
    cline_secrets = next(v for v in vscode if v.startswith("DEVCLOUD_CLINE_SECRETS_JSON="))
    assert "sk-user-own-key" in cline_secrets
    assert not any("shared-ai-token" in value for value in vscode)


@pytest.mark.asyncio
async def test_workspace_creation_sends_own_key_through_worker(
    client, db_session, fake_litellm, monkeypatch
):
    import app.worker_agent as worker_module

    admin = await _register(client, db_session, "genai-admin", admin=True)
    assert (await _configure(client, admin)).status_code == 200
    await _enable_workspace_ai(db_session)
    headers = await _register(client, db_session, "k015570")
    assert (await client.post("/api/genai/account", headers=headers)).status_code == 200

    received = []
    original = worker_module.podman_service.create_workspace_container

    async def capture(**kwargs):
        received.append(kwargs.get("ai_gateway_token"))
        return await original(**kwargs)

    monkeypatch.setattr(worker_module.podman_service, "create_workspace_container", capture)
    created = await client.post(
        "/api/workspaces",
        json={"name": "AI Notebook", "template_id": "jupyter-python", "flavor_id": "t1.micro"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    workspace_key = next(k["key"] for k in fake_litellm.keys.values() if k["purpose"] == "workspace")
    assert received == [workspace_key]

    # A user who never opted in keeps the shared key (no field value).
    other = await _register(client, db_session, "k099999")
    created = await client.post(
        "/api/workspaces",
        json={"name": "Shared AI", "template_id": "jupyter-python", "flavor_id": "t1.micro"},
        headers=other,
    )
    assert created.status_code == 201, created.text
    assert received[-1] == ""


@pytest.mark.asyncio
async def test_v26_genai_tables_receive_team_and_workspace_columns(tmp_path):
    from sqlalchemy import inspect as sa_inspect, text
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.migrations import _add_genai_teams_and_workspace_keys

    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'v26.db').as_posix()}")
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE genai_settings (id INTEGER PRIMARY KEY, enabled BOOLEAN)"))
            await conn.execute(text("CREATE TABLE genai_accounts (id INTEGER PRIMARY KEY, user_id INTEGER, litellm_user_id VARCHAR(128))"))
            await conn.execute(text("INSERT INTO genai_accounts (id, user_id, litellm_user_id) VALUES (1, 1, 'k015570')"))
            await _add_genai_teams_and_workspace_keys(conn)
            await _add_genai_teams_and_workspace_keys(conn)
            columns = await conn.run_sync(
                lambda sync: {
                    table: {c["name"] for c in sa_inspect(sync).get_columns(table)}
                    for table in ("genai_settings", "genai_accounts")
                }
            )
            row = (await conn.execute(text("SELECT workspace_key_team, encrypted_workspace_key FROM genai_accounts"))).one()
    finally:
        await engine.dispose()
    assert {"default_team", "team_priority_json"} <= columns["genai_settings"]
    assert {
        "personal_key_team", "workspace_key_alias", "workspace_key_token",
        "workspace_key_team", "encrypted_workspace_key",
    } <= columns["genai_accounts"]
    assert tuple(row) == ("", "")
