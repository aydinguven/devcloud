import asyncio
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.config import settings
from app.database import Base
from app.models.user import User
from app.models.node import Node, NodeStatus
from app.models.workspace import Workspace, WorkspaceStatus
from app.agents.manager import agent_manager
from app.orchestrator.podman_service import podman_service, PodmanService
from app.orchestrator.flavors import get_flavor
from app.orchestrator.templates import get_template
from app.orchestrator import idle_reaper
from app.routes import workspace_routes as routes
from app.schemas.workspace import WorkspaceCreate
from app.worker_agent import WorkerAgent
from tests.conftest import TEST_WORKER_ID, TestingSessionLocal
from tests.test_workspaces import get_authenticated_headers


async def create(client, headers, name="Safety workspace"):
    response = await client.post("/api/workspaces", headers=headers, json={
        "name": name, "template_id": "vscode-empty", "flavor_id": "t1.nano"})
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["stop", "delete"])
async def test_failed_operation_preserves_workspace_and_retries(client, db_session, tmp_path, monkeypatch, action):
    monkeypatch.setattr(settings, "STORAGE_ROOT", str(tmp_path))
    headers = await get_authenticated_headers(client, "failure_" + action)
    ws = await create(client, headers)
    sentinel = tmp_path / str(ws["user_id"]) / ws["id"] / "keep.txt"
    sentinel.write_text("user data")
    method = action + "_container"
    original = getattr(podman_service, method)
    monkeypatch.setattr(podman_service, method, AsyncMock(return_value=False))
    url = f"/api/workspaces/{ws['id']}" + ("/stop" if action == "stop" else "")
    request = client.post if action == "stop" else client.delete
    response = await request(url, headers=headers)
    assert response.status_code == 502
    stored = await db_session.get(Workspace, ws["id"], populate_existing=True)
    assert stored.status == WorkspaceStatus.RUNNING
    assert sentinel.read_text() == "user data"
    connection = agent_manager.get(TEST_WORKER_ID)
    assert not connection.agent.registry[ws["container_name"]].get("deleted")
    monkeypatch.setattr(podman_service, method, original)
    response = await request(url, headers=headers)
    assert response.status_code == 200
    if action == "stop":
        assert response.json()["status"] == "stopped"
        assert sentinel.exists()
    else:
        assert not sentinel.exists()
        assert await db_session.get(Workspace, ws["id"]) is None
        # A lost response can be retried even after a worker process restart.
        restarted = WorkerAgent()
        restarted.registry_path = connection.agent.registry_path
        restarted.registry = restarted._load_registry()
        result = await restarted.handle_container_command("container.delete", {"container_name": ws["container_name"]})
        assert result == {"success": True, "already_deleted": True}


@pytest.mark.asyncio
async def test_storage_cleanup_failure_keeps_registry_for_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_ROOT", str(tmp_path))
    agent = WorkerAgent()
    directory = tmp_path / "workspace"
    directory.mkdir()
    (directory / "data").write_text("keep")
    agent.registry = {"test": {"workspace_id": "id", "storage_path": str(directory)}}
    monkeypatch.setattr(podman_service, "delete_container", AsyncMock(return_value=True))
    import app.worker_agent as worker
    original = worker.shutil.rmtree
    monkeypatch.setattr(worker.shutil, "rmtree", lambda *_: (_ for _ in ()).throw(PermissionError("busy")))
    with pytest.raises(PermissionError):
        await agent.handle_container_command("container.delete", {"container_name": "test"})
    assert not agent.registry["test"].get("deleted")
    assert (directory / "data").exists()
    monkeypatch.setattr(worker.shutil, "rmtree", original)
    assert (await agent.handle_container_command("container.delete", {"container_name": "test"}))["success"]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["stop_container", "delete_container"])
@pytest.mark.parametrize("exit_code", [1, 125])
async def test_podman_absent_is_distinct_from_storage_failure(monkeypatch, action, exit_code):
    service = PodmanService()
    service._mock_mode = False
    command = AsyncMock(return_value=(exit_code, "", "storage unavailable"))
    monkeypatch.setattr(service, "run_cmd", command)
    if exit_code == 1:
        assert await getattr(service, action)("workspace") is True
    else:
        with pytest.raises(RuntimeError, match="determine"):
            await getattr(service, action)("workspace")
    assert command.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("limit", ["user", "worker"])
async def test_concurrent_admission_cannot_overbook(tmp_path, monkeypatch, backend, limit):
    if backend == "postgresql":
        url = os.environ.get("TEST_POSTGRES_URL")
        if not url:
            pytest.skip("Dedicated PostgreSQL test database not configured")
    else:
        url = "sqlite+aiosqlite:///" + str(tmp_path / "admission.db")
    # Deliberately bypass the per-process optimization: the database must
    # serialize these connections as if they belonged to separate processes.
    from app.orchestrator import admission
    class IndependentLocks:
        def setdefault(self, *_):
            return asyncio.Lock()
    monkeypatch.setattr(admission, "_admission_locks", IndependentLocks())
    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with sessions() as db:
        user = User(username="admission", email="admission@test.com", hashed_password="unused",
                    cpu_quota=1 if limit == "user" else 10, memory_mb_quota=10240, disk_mb_quota=10240)
        node = Node(id="admission-worker", name="admission-worker", enabled=True, schedulable=True,
                    status=NodeStatus.ONLINE, cpu_total=1 if limit == "worker" else 10, memory_total_mb=10240)
        db.add_all([user, node]); await db.commit(); user_id = user.id
    agent_manager._connections[node.id] = object()
    ready = asyncio.Event()
    arrivals = 0
    async def disk_usage(_):
        nonlocal arrivals
        arrivals += 1
        if arrivals == 2:
            ready.set()
        await asyncio.wait_for(ready.wait(), 5)
        return {}
    monkeypatch.setattr(routes, "get_workspace_disk_usage_by_user", disk_usage)
    async def reserve(name):
        async with sessions() as db:
            current_user = await db.get(User, user_id)
            return await routes.schedule_and_reserve_workspace(
                db, current_user=current_user, data=WorkspaceCreate(name=name, template_id="vscode-empty", flavor_id="t1.micro"),
                template=get_template("vscode-empty"), flavor=get_flavor("t1.micro"))
    try:
        outcomes = await asyncio.gather(reserve("first"), reserve("second"), return_exceptions=True)
        assert sum(isinstance(item, tuple) for item in outcomes) == 1, outcomes
        expected = routes.QuotaExceeded if limit == "user" else routes.NoSchedulableNode
        assert sum(isinstance(item, expected) for item in outcomes) == 1, outcomes
        async with sessions() as db:
            assert await db.scalar(select(func.count()).select_from(Workspace)) == 1
    finally:
        agent_manager._connections.pop(node.id, None)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()


@pytest.mark.asyncio
async def test_restart_rechecks_pinned_worker_capacity(client, db_session):
    node = await db_session.get(Node, TEST_WORKER_ID)
    node.cpu_total = 0.5
    node.memory_total_mb = 512
    await db_session.commit()
    headers = await get_authenticated_headers(client, "restart_capacity")
    first = await create(client, headers)
    assert (await client.post(f"/api/workspaces/{first['id']}/stop", headers=headers)).status_code == 200
    second = await create(client, headers, "second")
    response = await client.post(f"/api/workspaces/{first['id']}/start", headers=headers)
    assert response.status_code == 409
    stored = await db_session.get(Workspace, first["id"], populate_existing=True)
    assert stored.status == WorkspaceStatus.STOPPED
    assert second["status"] == "running"


@pytest.mark.asyncio
async def test_runtime_limit_boundary_disabled_and_failed_stop(client, db_session, monkeypatch):
    headers = await get_authenticated_headers(client, "runtime_limit")
    ws = await create(client, headers)
    stored = await db_session.get(Workspace, ws["id"])
    stored.auto_stop_minutes = 60
    stored.last_started_at = datetime.now(timezone.utc) - timedelta(minutes=59)
    await db_session.commit()
    monkeypatch.setattr(idle_reaper, "AsyncSessionLocal", TestingSessionLocal)
    assert await idle_reaper.run_idle_reaper_cycle() == 0
    stored.last_started_at = datetime.now(timezone.utc) - timedelta(minutes=61)
    await db_session.commit()
    original = podman_service.stop_container
    monkeypatch.setattr(podman_service, "stop_container", AsyncMock(return_value=False))
    assert await idle_reaper.run_idle_reaper_cycle() == 0
    await db_session.refresh(stored)
    assert stored.status == WorkspaceStatus.RUNNING
    stored.auto_stop_minutes = 0
    await db_session.commit()
    assert await idle_reaper.run_idle_reaper_cycle() == 0
    stored.auto_stop_minutes = 60
    await db_session.commit()
    monkeypatch.setattr(podman_service, "stop_container", original)
    assert await idle_reaper.run_idle_reaper_cycle() == 1


@pytest.mark.asyncio
async def test_start_cannot_overtake_inflight_stop(client, db_session, monkeypatch):
    from fastapi import HTTPException
    headers = await get_authenticated_headers(client, "stop_race")
    ws = await create(client, headers)
    entered = asyncio.Event()
    release = asyncio.Event()
    async def stop(_):
        entered.set()
        await release.wait()
        return True
    monkeypatch.setattr(podman_service, "stop_container", stop)
    async with TestingSessionLocal() as stop_db, TestingSessionLocal() as start_db:
        stopping_user = await stop_db.get(User, ws["user_id"])
        starting_user = await start_db.get(User, ws["user_id"])
        pending = asyncio.create_task(routes.stop_workspace_endpoint(ws["id"], stopping_user, stop_db))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            with pytest.raises(HTTPException) as error:
                await routes.start_workspace_endpoint(ws["id"], starting_user, start_db)
            assert error.value.status_code == 409
        finally:
            release.set()
            await pending
    await db_session.refresh(await db_session.get(Workspace, ws["id"]))
    assert (await db_session.get(Workspace, ws["id"])).status == WorkspaceStatus.STOPPED


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [WorkspaceStatus.CREATING, WorkspaceStatus.STARTING, WorkspaceStatus.STOPPING])
async def test_controller_restart_recovers_interrupted_operations(client, db_session, monkeypatch, state):
    import app.main as main_module
    headers = await get_authenticated_headers(client, "recover_" + state.value)
    ws = await create(client, headers)
    stored = await db_session.get(Workspace, ws["id"])
    stored.status = state
    await db_session.commit()
    monkeypatch.setattr(main_module, "AsyncSessionLocal", TestingSessionLocal)
    await main_module.recover_interrupted_workspace_operations()
    await db_session.refresh(stored)
    assert stored.status == WorkspaceStatus.ERROR
    assert "retry" in stored.error_message
