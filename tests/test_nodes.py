import pytest
from httpx import AsyncClient
from sqlalchemy import update

from app.models.node import Node, NodeStatus
from app.models.user import User, UserRole
from app.orchestrator.flavors import get_flavor
from app.orchestrator.scheduler import NoSchedulableNode, select_worker_node
from tests.conftest import TestingSessionLocal


async def _admin_headers(client: AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/auth/register",
        json={"username": "node_admin", "email": "node-admin@test.com", "password": "Password123!"},
    )
    token = response.json()["access_token"]
    user_id = response.json()["user"]["id"]
    async with TestingSessionLocal() as session:
        await session.execute(update(User).where(User.id == user_id).values(role=UserRole.ADMIN))
        await session.commit()
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_admin_can_enroll_and_drain_worker_without_revealing_token_later(client: AsyncClient):
    headers = await _admin_headers(client)
    created = await client.post(
        "/api/admin/nodes",
        headers=headers,
        json={"name": "cpu-worker-01", "schedulable": True, "labels": {"zone": "dc1"}},
    )
    assert created.status_code == 201
    body = created.json()
    assert body["enrollment_token"]
    assert body["status"] == "pending"

    listed = await client.get("/api/admin/nodes", headers=headers)
    assert listed.status_code == 200
    assert listed.json()[0]["name"] == "cpu-worker-01"
    assert "enrollment_token" not in listed.json()[0]

    drained = await client.patch(
        f"/api/admin/nodes/{body['id']}",
        headers=headers,
        json={"schedulable": False},
    )
    assert drained.status_code == 200
    assert drained.json()["schedulable"] is False


@pytest.mark.asyncio
async def test_scheduler_uses_online_cpu_worker_and_never_falls_back_when_workers_exist(db_session, monkeypatch):
    worker = Node(
        name="cpu-worker-02",
        status=NodeStatus.ONLINE,
        enabled=True,
        schedulable=True,
        cpu_total=8,
        memory_total_mb=16384,
        disk_total_mb=100000,
        agent_token_hash="0" * 64,
    )
    db_session.add(worker)
    await db_session.commit()

    monkeypatch.setattr(
        "app.orchestrator.scheduler.agent_manager.is_connected",
        lambda node_id: node_id == worker.id,
    )

    selected = await select_worker_node(db_session, get_flavor("t1.small"))
    assert selected.id == worker.id

    worker.status = NodeStatus.OFFLINE
    db_session.add(worker)
    await db_session.commit()
    with pytest.raises(NoSchedulableNode):
        await select_worker_node(db_session, get_flavor("t1.small"))
