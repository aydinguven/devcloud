import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app import health
from app.config import settings
from app.models.node import Node, NodeStatus


def _node(**overrides):
    node = {
        "id": "n1",
        "name": "w1",
        "enabled": True,
        "schedulable": True,
        "connected": True,
        "last_seen_at": None,
        "agent_version": settings.APP_VERSION,
        "capabilities": {},
        "reconciliation": {},
    }
    node.update(overrides)
    return node


def test_optional_component_cannot_take_the_platform_down():
    optional_down = [
        {"required": True, "status": "ok"},
        {"required": False, "status": "down"},
        {"required": False, "status": "disabled"},
    ]
    assert health.overall_status(optional_down) == "degraded"
    assert health.overall_status(
        [*optional_down, {"required": True, "status": "down"}]
    ) == "down"


def test_worker_classification_by_heartbeat_and_self_check():
    now = datetime.now(timezone.utc)

    def status(**overrides):
        return health.classify_worker(_node(**overrides), now, settings.APP_VERSION)["status"]

    assert status(last_seen_at=now - timedelta(seconds=20)) == "ok"
    assert status(last_seen_at=now - timedelta(seconds=90)) == "degraded"
    assert status(last_seen_at=now - timedelta(seconds=150)) == "down"
    assert status(connected=False, last_seen_at=now) == "down"
    assert status(enabled=False, connected=False) == "disabled"
    broken_podman = {"health": {"podman": {"ok": False, "message": "x"}, "storage": {"ok": True}}}
    assert status(last_seen_at=now, capabilities=broken_podman) == "down"


@pytest.mark.asyncio
async def test_hanging_check_times_out(monkeypatch):
    import app.auth.ldap as ldap

    monkeypatch.setattr(health, "CHECK_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(ldap, "test_directory_configuration", lambda config: time.sleep(2))
    snapshot = health._Snapshot(
        directory=SimpleNamespace(server_host="ldap.example", server_port=389)
    )
    started = time.monotonic()
    component = await health.check_directory(snapshot)
    assert time.monotonic() - started < 1.5
    assert component.status == "down"


@pytest.mark.asyncio
async def test_public_report_hides_details_and_signals_outage(client, db_session):
    worker = await db_session.get(Node, "00000000-0000-0000-0000-000000000001")
    worker.enabled = False
    worker.status = NodeStatus.OFFLINE
    await db_session.commit()
    health.reset_cache()

    response = await client.get("/api/health")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "down"
    for component in body["components"]:
        assert set(component) == {"key", "label", "required", "status", "summary", "latency_ms"}
    health.reset_cache()
