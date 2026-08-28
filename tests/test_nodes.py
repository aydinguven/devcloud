import hashlib

import pytest
from fastapi import WebSocketDisconnect
from httpx import AsyncClient
from sqlalchemy import update

from app.models.node import Node, NodeStatus
from app.models.user import User, UserRole
from app.models.workspace import Workspace, WorkspaceStatus
from app.orchestrator.flavors import get_flavor
from app.orchestrator.scheduler import NoSchedulableNode, select_worker_node
from tests.conftest import TEST_WORKER_ID
from app.routes.agent_routes import connect_agent
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


@pytest.mark.asyncio
async def test_agent_heartbeat_preserves_admin_drain_change(db_session):
    token = "node-secret"
    worker = Node(
        name="cpu-worker-drain",
        status=NodeStatus.PENDING,
        enabled=True,
        schedulable=True,
        agent_token_hash=hashlib.sha256(token.encode()).hexdigest(),
    )
    db_session.add(worker)
    await db_session.commit()
    await db_session.refresh(worker)

    class FakeWebSocket:
        headers = {"authorization": f"Bearer {token}"}

        def __init__(self):
            self.receive_count = 0
            self.status_during_connection = None

        async def accept(self):
            return None

        async def close(self, **_kwargs):
            return None

        async def receive_json(self):
            self.receive_count += 1
            if self.receive_count == 1:
                async with TestingSessionLocal() as admin_session:
                    await admin_session.execute(
                        update(Node)
                        .where(Node.id == worker.id)
                        .values(schedulable=False, status=NodeStatus.DRAINING)
                    )
                    await admin_session.commit()
                return {
                    "type": "heartbeat",
                    "payload": {
                        "hostname": "worker-drain",
                        "cpu_total": 8,
                        "memory_total_mb": 16384,
                        "disk_total_mb": 100000,
                        "capabilities": {"runtime": "podman"},
                        "agent_version": "test",
                    },
                }
            async with TestingSessionLocal() as observer:
                fresh = await observer.get(Node, worker.id)
                self.status_during_connection = fresh.status
            raise WebSocketDisconnect()

    websocket = FakeWebSocket()
    await connect_agent(websocket, worker.id, db_session)

    assert websocket.status_during_connection == NodeStatus.DRAINING


@pytest.mark.asyncio
async def test_admin_can_delete_worker(client: AsyncClient, db_session):
    headers = await _admin_headers(client)
    created = await client.post(
        "/api/admin/nodes",
        headers=headers,
        json={"name": "cpu-worker-delete-me", "schedulable": True, "labels": {}},
    )
    assert created.status_code == 201
    node_id = created.json()["id"]

    delete_resp = await client.delete(f"/api/admin/nodes/{node_id}", headers=headers)
    assert delete_resp.status_code == 200
    assert "başarıyla silindi" in delete_resp.json()["message"]

    get_resp = await client.get("/api/admin/nodes", headers=headers)
    assert get_resp.status_code == 200
    assert not any(n["id"] == node_id for n in get_resp.json())


@pytest.mark.asyncio
async def test_admin_delete_worker_not_found(client: AsyncClient):
    headers = await _admin_headers(client)
    delete_resp = await client.delete("/api/admin/nodes/non-existent-id", headers=headers)
    assert delete_resp.status_code == 404


@pytest.mark.asyncio
async def test_admin_cannot_delete_worker_with_active_workspaces(client: AsyncClient, db_session):
    headers = await _admin_headers(client)
    created = await client.post(
        "/api/admin/nodes",
        headers=headers,
        json={"name": "cpu-worker-busy", "schedulable": True, "labels": {}},
    )
    node_id = created.json()["id"]

    # Create active workspace assigned to this node
    ws = Workspace(
        name="test-ws",
        user_id=1,
        node_id=node_id,
        template_id="vscode-python",
        flavor_id="t1.small",
        container_name="cnt-test-ws",
        host_port=20001,
        storage_path="/tmp/test-ws",
        status=WorkspaceStatus.RUNNING,
    )
    db_session.add(ws)
    await db_session.commit()

    delete_resp = await client.delete(f"/api/admin/nodes/{node_id}", headers=headers)
    assert delete_resp.status_code == 409
    assert "atanmış" in delete_resp.json()["detail"]


@pytest.mark.asyncio
async def test_admin_cannot_delete_worker_with_stopped_workspaces(client: AsyncClient, db_session):
    headers = await _admin_headers(client)
    created = await client.post(
        "/api/admin/nodes",
        headers=headers,
        json={"name": "cpu-worker-idle", "schedulable": True, "labels": {}},
    )
    node_id = created.json()["id"]

    # Create stopped workspace assigned to this node
    ws = Workspace(
        name="test-stopped-ws",
        user_id=1,
        node_id=node_id,
        template_id="vscode-python",
        flavor_id="t1.small",
        container_name="cnt-test-stopped-ws",
        host_port=20002,
        storage_path="/tmp/test-stopped-ws",
        status=WorkspaceStatus.STOPPED,
    )
    db_session.add(ws)
    await db_session.commit()

    delete_resp = await client.delete(f"/api/admin/nodes/{node_id}", headers=headers)
    assert delete_resp.status_code == 409
    assert "atanmış" in delete_resp.json()["detail"]


@pytest.mark.asyncio
async def test_admin_can_update_node_labels(client: AsyncClient):
    headers = await _admin_headers(client)
    created = await client.post(
        "/api/admin/nodes",
        headers=headers,
        json={"name": "cpu-worker-labels", "schedulable": True, "labels": {"zone": "dc1"}},
    )
    assert created.status_code == 201
    node_id = created.json()["id"]

    updated = await client.put(
        f"/api/admin/nodes/{node_id}/labels",
        headers=headers,
        json={"labels": {"zone": "dc2", "gpu": "true", "tier": "fast"}},
    )
    assert updated.status_code == 200
    assert updated.json()["labels"] == {"zone": "dc2", "gpu": "true", "tier": "fast"}


@pytest.mark.asyncio
async def test_scheduler_load_balancing_and_affinity(db_session, monkeypatch):
    # Worker 1: Heavily loaded (high CPU/RAM utilization), zone=dmz
    w1 = Node(
        name="worker-heavy",
        status=NodeStatus.ONLINE,
        enabled=True,
        schedulable=True,
        cpu_total=8,
        memory_total_mb=16384,
        disk_total_mb=100000,
        cpu_percent=85.0,
        memory_used_mb=14000,
        labels_json='{"zone": "dmz"}',
        agent_token_hash="1" * 64,
    )
    # Worker 2: Lightly loaded (low utilization), zone=dmz
    w2 = Node(
        name="worker-light",
        status=NodeStatus.ONLINE,
        enabled=True,
        schedulable=True,
        cpu_total=8,
        memory_total_mb=16384,
        disk_total_mb=100000,
        cpu_percent=10.0,
        memory_used_mb=2000,
        labels_json='{"zone": "dmz"}',
        agent_token_hash="2" * 64,
    )
    # Worker 3: Lightly loaded, but zone=internal
    w3 = Node(
        name="worker-internal",
        status=NodeStatus.ONLINE,
        enabled=True,
        schedulable=True,
        cpu_total=8,
        memory_total_mb=16384,
        disk_total_mb=100000,
        cpu_percent=5.0,
        memory_used_mb=1000,
        labels_json='{"zone": "internal"}',
        agent_token_hash="3" * 64,
    )
    db_session.add_all([w1, w2, w3])
    await db_session.commit()

    monkeypatch.setattr(
        "app.orchestrator.scheduler.agent_manager.is_connected",
        lambda node_id: True,
    )

    # 1. Without selector: scheduler picks worker-internal (lowest composite load)
    selected = await select_worker_node(db_session, get_flavor("t1.small"))
    assert selected.id == w3.id

    # 2. With affinity selector {"zone": "dmz"}: scheduler picks worker-light over worker-heavy
    selected_dmz = await select_worker_node(
        db_session, get_flavor("t1.small"), node_selector={"zone": "dmz"}
    )
    assert selected_dmz.id == w2.id

    # 3. With non-existent affinity selector: raises NoSchedulableNode
    with pytest.raises(NoSchedulableNode):
        await select_worker_node(
            db_session, get_flavor("t1.small"), node_selector={"zone": "gpu-farm"}
        )


@pytest.mark.asyncio
async def test_admin_cannot_metadata_only_migrate_workspace(client: AsyncClient, db_session):
    headers = await _admin_headers(client)
    n1 = Node(
        name="worker-mig-1",
        status=NodeStatus.ONLINE,
        enabled=True,
        schedulable=True,
        cpu_total=8,
        memory_total_mb=16384,
        disk_total_mb=100000,
        agent_token_hash="a" * 64,
    )
    n2 = Node(
        name="worker-mig-2",
        status=NodeStatus.ONLINE,
        enabled=True,
        schedulable=True,
        cpu_total=8,
        memory_total_mb=16384,
        disk_total_mb=100000,
        agent_token_hash="b" * 64,
    )
    db_session.add_all([n1, n2])
    await db_session.commit()

    ws = Workspace(
        name="migrate-ws",
        user_id=1,
        node_id=n1.id,
        template_id="vscode-python",
        flavor_id="t1.small",
        container_name="cnt-mig-ws",
        host_port=20010,
        storage_path="/tmp/mig-ws",
        status=WorkspaceStatus.STOPPED,
    )
    db_session.add(ws)
    await db_session.commit()

    # Reassigning only node_id would strand the data on n1, so the API refuses.
    res = await client.post(
        f"/api/admin/workspaces/{ws.id}/migrate?target_node_id={n2.id}",
        headers=headers,
    )
    assert res.status_code == 501

    await db_session.refresh(ws)
    assert ws.node_id == n1.id


@pytest.mark.asyncio
async def test_admin_cannot_migrate_running_workspace(client: AsyncClient, db_session):
    headers = await _admin_headers(client)
    ws = Workspace(
        name="active-mig-ws",
        user_id=1,
        node_id=TEST_WORKER_ID,
        template_id="vscode-python",
        flavor_id="t1.small",
        container_name="cnt-active-mig",
        host_port=20011,
        storage_path="/tmp/active-mig",
        status=WorkspaceStatus.RUNNING,
    )
    db_session.add(ws)
    await db_session.commit()

    res = await client.post(
        f"/api/admin/workspaces/{ws.id}/migrate",
        headers=headers,
    )
    assert res.status_code == 501
    assert "taşıma" in res.json()["detail"]


@pytest.mark.asyncio
async def test_admin_upgrade_node_offline_error(client: AsyncClient, db_session):
    headers = await _admin_headers(client)
    n = Node(
        name="worker-upgrade-test",
        status=NodeStatus.OFFLINE,
        enabled=True,
        schedulable=True,
        agent_token_hash="u" * 64,
    )
    db_session.add(n)
    await db_session.commit()

    res = await client.post(f"/api/admin/nodes/{n.id}/upgrade", headers=headers)
    assert res.status_code == 400
    assert "çevrimdışı" in res.json()["detail"]


@pytest.mark.asyncio
async def test_agent_manager_event_broadcasting():
    from app.agents.manager import agent_manager
    queue = agent_manager.subscribe_events()
    try:
        await agent_manager.broadcast_event(
            "node.connected", {"node_id": "test-node-1", "status": "online"}
        )
        event = queue.get_nowait()
        assert event["type"] == "node.connected"
        assert event["data"]["node_id"] == "test-node-1"
        assert event["data"]["status"] == "online"
    finally:
        agent_manager.unsubscribe_events(queue)



