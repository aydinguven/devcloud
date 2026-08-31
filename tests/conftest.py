import asyncio
import base64
import os
import uuid
from pathlib import Path
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from app.config import settings
from app.database import Base, get_db
from app.models.user import User
from app.models.workspace import Workspace
from app.models.node import Node, NodeStatus
from app.models.custom_template import CustomTemplate
from app.models.directory_settings import DirectorySettings
from app.models.jupyter_ai_settings import JupyterAiSettings
from app.main import app
from app.orchestrator.podman_service import podman_service
from app.agents.manager import AgentCommandError, AgentStream, StreamChunk, agent_manager
from app.worker_agent import WorkerAgent

# Force test mode and mock podman
settings.USE_MOCK_PODMAN = True
podman_service._mock_mode = True

# Test in-memory database
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
test_engine = create_async_engine(
    TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
)
TestingSessionLocal = async_sessionmaker(
    bind=test_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)

TEST_WORKER_ID = "00000000-0000-0000-0000-000000000001"


class InProcessWorkerConnection:
    """Exercise the production worker command surface without a real socket."""

    def __init__(self, agent: WorkerAgent, db_session: AsyncSession):
        self.agent = agent
        self.db_session = db_session

    async def _ensure_registry(self, payload: dict) -> None:
        container_name = str(payload.get("container_name") or "")
        if not container_name or container_name in self.agent.registry:
            return
        from sqlalchemy import select

        workspace = (
            await self.db_session.execute(
                select(Workspace).where(Workspace.container_name == container_name)
            )
        ).scalar_one_or_none()
        if workspace:
            self.agent.registry[container_name] = {
                "workspace_id": workspace.id,
                "container_id": workspace.container_id or "",
                "storage_path": workspace.storage_path,
                "host_port": workspace.host_port,
            }

    async def request(self, action: str, payload: dict, timeout: float = 60) -> dict:
        await self._ensure_registry(payload)
        if action.startswith("files."):
            return await self.agent.handle_file_command(action, payload)
        if action.startswith("system."):
            return await self.agent.handle_system_command(action, payload)
        return await self.agent.handle_container_command(action, payload)

    async def open_stream(
        self, action: str, payload: dict, timeout: float = 30
    ) -> tuple[dict, AgentStream]:
        await self._ensure_registry(payload)
        stream = AgentStream(str(uuid.uuid4()))
        if action == "workspace.backup.open":
            import tempfile
            from app.orchestrator.backup_service import create_workspace_zip_backup

            entry = self.agent._registered_entry(
                str(payload["container_name"]), str(payload["workspace_id"])
            )
            descriptor, archive_name = tempfile.mkstemp(suffix=".zip")
            os.close(descriptor)
            archive = Path(archive_name)
            try:
                create_workspace_zip_backup(entry["storage_path"], archive)
                content = archive.read_bytes()
            finally:
                archive.unlink(missing_ok=True)
            await stream.queue.put(StreamChunk(content))
            await stream.queue.put(None)
            return {"filename": "backup.zip", "size": len(content)}, stream

        if action != "proxy.http.open":
            raise AgentCommandError(f"Unsupported test stream action: {action}")

        import app.proxy.router as proxy_module

        try:
            target_url = await self.agent._target_url(payload, websocket=False)
            body = base64.b64decode(payload.get("body", ""), validate=True)
            client = proxy_module.httpx.AsyncClient(
                timeout=30.0, follow_redirects=False
            )
            request = client.build_request(
                method=payload["method"],
                url=target_url,
                headers=payload.get("headers") or {},
                content=body,
            )
            response = await client.send(request, stream=True)
            if hasattr(response.headers, "multi_items"):
                headers = list(response.headers.multi_items())
            else:
                headers = list(response.headers.items())
            async for chunk in response.aiter_raw():
                await stream.queue.put(StreamChunk(chunk))
            await stream.queue.put(None)
            await response.aclose()
            await client.aclose()
            return {"status_code": response.status_code, "headers": headers}, stream
        except Exception as exc:
            raise AgentCommandError(str(exc)) from exc


@pytest_asyncio.fixture(scope="function")
async def db_session():
    """Create fresh database tables for each test function."""
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with TestingSessionLocal() as session:
        yield session

    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture(scope="function")
async def client(db_session: AsyncSession, tmp_path: Path):
    """Async HTTP test client overriding get_db dependency."""
    async def override_get_db():
        yield db_session

    worker = Node(
        id=TEST_WORKER_ID,
        name="test-worker",
        status=NodeStatus.ONLINE,
        cpu_total=64,
        memory_total_mb=262144,
        disk_total_mb=1048576,
    )
    db_session.add(worker)
    await db_session.commit()
    agent = WorkerAgent()
    agent.registry_path = tmp_path / "worker-registry.json"
    agent.registry = {}
    connection = InProcessWorkerConnection(agent, db_session)
    agent_manager._connections[TEST_WORKER_ID] = connection

    app.dependency_overrides[get_db] = override_get_db
    
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()
    if agent_manager._connections.get(TEST_WORKER_ID) is connection:
        agent_manager._connections.pop(TEST_WORKER_ID, None)
