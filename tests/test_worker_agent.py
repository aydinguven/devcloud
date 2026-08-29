import json

import pytest

from app.config import settings
from app.worker_agent import WorkerAgent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,payload",
    [
        ("container.exists", {}),
        ("container.start", {}),
        ("container.stop", {}),
        ("container.status", {}),
        ("container.logs", {}),
        ("container.port_ready", {"host_port": 10100}),
        ("container.stats", {}),
        ("container.storage_size", {}),
        ("container.delete", {}),
    ],
)
async def test_worker_rejects_lifecycle_commands_for_unregistered_containers(
    action,
    payload,
):
    agent = WorkerAgent()
    agent.registry = {}

    with pytest.raises(PermissionError, match="kayıtlı değil"):
        await agent.handle_container_command(
            action,
            {"container_name": "unmanaged-container", **payload},
        )


@pytest.mark.asyncio
async def test_worker_proxy_requires_workspace_container_match():
    agent = WorkerAgent()
    agent.registry = {
        "devcloud-1-aabbccdd": {
            "workspace_id": "workspace-a",
            "storage_path": "/tmp/workspace-a",
            "host_port": 10100,
        }
    }

    with pytest.raises(PermissionError, match="eşleşmesi"):
        await agent._target_url(
            {
                "workspace_id": "workspace-b",
                "container_name": "devcloud-1-aabbccdd",
                "host_port": 10100,
                "path": "",
                "query": "",
            },
            websocket=False,
        )


@pytest.mark.asyncio
async def test_worker_agent_system_upgrade_initiates_upgrade(monkeypatch):
    monkeypatch.setenv("DEVCLOUD_MASTER_URL", "https://master.devcloud.local")
    monkeypatch.setenv("DEVCLOUD_NODE_ID", "node-upgrade-123")
    monkeypatch.setenv("DEVCLOUD_NODE_TOKEN", "token-xyz")

    agent = WorkerAgent()
    # Stub _execute_upgrade to not run shell subprocess in test
    executed = False

    async def fake_execute(master_url):
        nonlocal executed
        executed = True

    monkeypatch.setattr(agent, "_execute_upgrade", fake_execute)

    res = await agent.handle_system_command("system.upgrade", {})
    assert res["status"] == "upgrade_started"
    assert agent.upgrade_status["state"] == "preparing"


def test_worker_reports_durable_upgrade_queue_status(tmp_path, monkeypatch):
    queue = tmp_path / "update-queue"
    queue.mkdir()
    monkeypatch.setattr(settings, "UPDATE_QUEUE_ROOT", str(queue))
    (queue / "pending.json").write_text(
        json.dumps(
            {
                "state": "queued",
                "target_version": "3.4.5",
                "queued_at": "2026-08-29T20:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    status = WorkerAgent()._reported_upgrade_status()

    assert status["state"] == "queued"
    assert status["target_version"] == "3.4.5"

