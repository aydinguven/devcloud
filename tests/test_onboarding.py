from datetime import datetime, timezone

import pytest
from sqlalchemy import inspect

from app.models.mlflow_server_settings import MlflowServerSettings
from app.models.onboarding import OnboardingProgress, OnboardingSettings
from app.models.user import User, UserRole
from app.models.workspace import Workspace, WorkspaceStatus
from tests.conftest import TEST_WORKER_ID


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def register(client, username: str):
    response = await client.post(
        "/api/auth/register",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": "Password123!",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.asyncio
async def test_admin_controls_versioned_optional_tour(client, db_session):
    endpoint = "/api/admin/onboarding-settings"
    assert (await client.get(endpoint)).status_code == 401

    existing = await register(client, "tour_existing")
    existing_headers = bearer(existing["access_token"])
    assert (await client.get(endpoint, headers=existing_headers)).status_code == 403

    existing_user = await db_session.get(User, existing["user"]["id"])
    existing_user.role = UserRole.ADMIN
    await db_session.commit()
    assert (await client.get(endpoint, headers=existing_headers)).json() == {
        "enabled": False,
        "current_version": 1,
        "enabled_at": None,
        "updated_at": None,
    }
    for invalid in (
        {"enabled": "true", "current_version": 1},
        {"enabled": True, "current_version": 0},
        {"enabled": True, "current_version": "2"},
    ):
        assert (await client.put(endpoint, headers=existing_headers, json=invalid)).status_code == 422

    enabled = await client.put(
        endpoint,
        headers=existing_headers,
        json={"enabled": True, "current_version": 1},
    )
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["enabled"] is True
    assert enabled.json()["enabled_at"]

    # Accounts created before enablement are not interrupted automatically.
    existing_state = await client.get("/api/onboarding/state", headers=existing_headers)
    assert existing_state.status_code == 200
    assert existing_state.json()["auto_offer"] is False
    assert existing_state.json()["features"]["admin"] is True

    new_user = await register(client, "tour_new")
    user_headers = bearer(new_user["access_token"])
    fresh = (await client.get("/api/onboarding/state", headers=user_headers)).json()
    assert fresh["enabled"] is True
    assert fresh["auto_offer"] is True
    assert fresh["status"] == "not_started"
    assert fresh["topic_choices"] == {}
    assert fresh["features"]["mlflow"] is False
    assert fresh["features"]["workspace-detail"] is True

    started = await client.patch(
        "/api/onboarding/state",
        headers=user_headers,
        json={
            "tour_version": 1,
            "expected_revision": 0,
            "status": "in_progress",
            "current_topic": "workspace-create",
            "current_step": "ws-create-open",
        },
    )
    assert started.status_code == 200, started.text
    started_state = started.json()
    assert started_state["current_step"] == "ws-create-open"
    assert started_state["topic_choices"] == {}
    assert started_state["revision"] == 1

    stale = await client.patch(
        "/api/onboarding/state",
        headers=user_headers,
        json={
            "tour_version": 1,
            "expected_revision": 0,
            "status": "paused",
        },
    )
    assert stale.status_code == 409

    unavailable = await client.patch(
        "/api/onboarding/state",
        headers=user_headers,
        json={
            "tour_version": 1,
            "expected_revision": 1,
            "topic_choice": {"topic_id": "mlflow", "choice": "show"},
        },
    )
    assert unavailable.status_code == 409
    skipped = await client.patch(
        "/api/onboarding/state",
        headers=user_headers,
        json={
            "tour_version": 1,
            "expected_revision": 1,
            "topic_choice": {"topic_id": "mlflow", "choice": "skip"},
        },
    )
    assert skipped.status_code == 200
    skipped_state = skipped.json()
    assert skipped_state["topic_choices"]["mlflow"] == "skip"

    restarted = await client.post(
        "/api/onboarding/restart",
        headers=user_headers,
        json={
            "tour_version": 1,
            "expected_revision": skipped_state["revision"],
        },
    )
    assert restarted.status_code == 200
    assert restarted.json()["status"] == "not_started"
    assert restarted.json()["topic_choices"] == {}

    disabled = await client.put(
        endpoint,
        headers=existing_headers,
        json={"enabled": False, "current_version": 1},
    )
    assert disabled.status_code == 200
    disabled_state = (await client.get("/api/onboarding/state", headers=user_headers)).json()
    assert disabled_state["enabled"] is False
    assert (await db_session.get(OnboardingProgress, new_user["user"]["id"])) is not None
    blocked = await client.patch(
        "/api/onboarding/state",
        headers=user_headers,
        json={
            "tour_version": 1,
            "expected_revision": disabled_state["revision"],
            "status": "in_progress",
        },
    )
    assert blocked.status_code == 409
    restart_blocked = await client.post(
        "/api/onboarding/restart",
        headers=user_headers,
        json={
            "tour_version": 1,
            "expected_revision": disabled_state["revision"],
        },
    )
    assert restart_blocked.status_code == 409

    version_two = await client.put(
        endpoint,
        headers=existing_headers,
        json={"enabled": True, "current_version": 2},
    )
    assert version_two.status_code == 200
    assert (
        await client.put(
            endpoint,
            headers=existing_headers,
            json={"enabled": True, "current_version": 1},
        )
    ).status_code == 422
    refreshed = (await client.get("/api/onboarding/state", headers=user_headers)).json()
    assert refreshed["tour_version"] == 2
    assert refreshed["status"] == "not_started"
    assert refreshed["topic_choices"] == {}
    assert refreshed["auto_offer"] is True

    page = await client.get("/admin/system", headers=existing_headers)
    assert page.status_code == 200
    assert 'id="onboarding-settings-form"' in page.text
    assert 'name="current_version"' in page.text


@pytest.mark.asyncio
async def test_tour_capabilities_follow_user_context(client, db_session):
    admin = await register(client, "tour_cap_admin")
    admin_headers = bearer(admin["access_token"])
    admin_user = await db_session.get(User, admin["user"]["id"])
    admin_user.role = UserRole.ADMIN
    settings = OnboardingSettings(
        id=1,
        enabled=True,
        current_version=1,
        enabled_at=datetime.now(timezone.utc),
    )
    db_session.add(settings)
    await db_session.commit()

    user = await register(client, "tour_cap_user")
    user_headers = bearer(user["access_token"])
    initial = (await client.get("/api/onboarding/state", headers=user_headers)).json()
    assert initial["features"]["mlflow"] is False
    assert initial["features"]["workspace-detail"] is True
    assert initial["context"]["first_workspace_url"] is None
    dashboard = await client.get("/", headers=user_headers)
    assert dashboard.status_code == 200
    assert 'data-tour="workspace-demo-overview"' in dashboard.text
    assert 'data-tour="workspace-demo-metrics"' in dashboard.text
    assert 'data-tour="workspace-demo-logs"' in dashboard.text
    assert 'data-tour="workspace-demo-files"' in dashboard.text
    assert 'data-tour="workspace-demo-ports"' in dashboard.text
    assert "Demo · Gerçek kaynak oluşturmaz" in dashboard.text

    db_session.add(
        MlflowServerSettings(
            id=1,
            enabled=True,
            base_url="https://mlflow.example.com",
        )
    )
    workspace = Workspace(
        name="Tour workspace",
        user_id=user["user"]["id"],
        node_id=TEST_WORKER_ID,
        template_id="vscode-python",
        flavor_id="t1.micro",
        container_name="tour-cap-workspace",
        host_port=19222,
        storage_path="/tmp/tour-cap-workspace",
        status=WorkspaceStatus.STOPPED,
    )
    db_session.add(workspace)
    await db_session.commit()

    updated = (await client.get("/api/onboarding/state", headers=user_headers)).json()
    assert updated["features"]["mlflow"] is True
    assert updated["features"]["workspace-detail"] is True
    assert updated["features"]["admin"] is False
    assert updated["context"]["first_workspace_url"] == f"/workspaces/{workspace.id}"

    # Admins and users only receive capabilities for their own account context.
    admin_state = (await client.get("/api/onboarding/state", headers=admin_headers)).json()
    assert admin_state["features"]["admin"] is True
    assert admin_state["features"]["workspace-detail"] is True

    deleted_workspace = Workspace(
        name="Deleted tour workspace",
        user_id=admin["user"]["id"],
        node_id=TEST_WORKER_ID,
        template_id="vscode-python",
        flavor_id="t1.micro",
        container_name="deleted-tour-workspace",
        host_port=19223,
        storage_path="/tmp/deleted-tour-workspace",
        status=WorkspaceStatus.DELETED,
    )
    db_session.add(deleted_workspace)
    await db_session.commit()
    deleted_only_state = (
        await client.get("/api/onboarding/state", headers=admin_headers)
    ).json()
    assert deleted_only_state["context"]["first_workspace_url"] is None
    deleted_dashboard = await client.get("/", headers=admin_headers)
    assert 'data-tour="workspace-demo-overview"' in deleted_dashboard.text


@pytest.mark.asyncio
async def test_onboarding_tables_are_registered_with_user_cascade(db_session):
    connection = await db_session.connection()
    tables = await connection.run_sync(lambda conn: set(inspect(conn).get_table_names()))
    foreign_keys = await connection.run_sync(
        lambda conn: inspect(conn).get_foreign_keys("onboarding_progress")
    )

    assert {"onboarding_settings", "onboarding_progress"} <= tables
    user_fk = next(
        item for item in foreign_keys if item["constrained_columns"] == ["user_id"]
    )
    assert user_fk["referred_table"] == "users"
    assert user_fk["options"].get("ondelete") == "CASCADE"



@pytest.mark.asyncio
async def test_onboarding_step_ids_validate_and_legacy_state_remains_compatible(
    client, db_session
):
    db_session.add(
        OnboardingSettings(
            id=1,
            enabled=True,
            current_version=1,
            enabled_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()
    user = await register(client, "tour_step_user")
    headers = bearer(user["access_token"])

    unknown = await client.patch(
        "/api/onboarding/state",
        headers=headers,
        json={
            "tour_version": 1,
            "expected_revision": 0,
            "status": "in_progress",
            "current_topic": "workspace-create",
            "current_step": "not-a-tour-step",
        },
    )
    assert unknown.status_code == 422

    mismatch = await client.patch(
        "/api/onboarding/state",
        headers=headers,
        json={
            "tour_version": 1,
            "expected_revision": 0,
            "status": "in_progress",
            "current_topic": "profile",
            "current_step": "ws-create-open",
        },
    )
    assert mismatch.status_code == 422

    legacy = await client.patch(
        "/api/onboarding/state",
        headers=headers,
        json={
            "tour_version": 1,
            "expected_revision": 0,
            "status": "in_progress",
            "current_topic": "workspace-create",
            "current_step": "highlight",
        },
    )
    assert legacy.status_code == 200
    assert legacy.json()["current_step"] == "highlight"

    stable = await client.patch(
        "/api/onboarding/state",
        headers=headers,
        json={
            "tour_version": 1,
            "expected_revision": legacy.json()["revision"],
            "current_topic": "workspace-create",
            "current_step": "ws-create-open",
        },
    )
    assert stable.status_code == 200
    assert stable.json()["current_step"] == "ws-create-open"

    partial_topic = await client.patch(
        "/api/onboarding/state",
        headers=headers,
        json={
            "tour_version": 1,
            "expected_revision": stable.json()["revision"],
            "current_topic": "profile",
        },
    )
    assert partial_topic.status_code == 422

    partial_step = await client.patch(
        "/api/onboarding/state",
        headers=headers,
        json={
            "tour_version": 1,
            "expected_revision": stable.json()["revision"],
            "current_step": "profile-details",
        },
    )
    assert partial_step.status_code == 422
