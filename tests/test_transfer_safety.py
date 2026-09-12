import asyncio
import base64
import hashlib
import io
from unittest.mock import AsyncMock

import pytest

from app.agents.manager import AgentConnection, AgentCommandError, AgentUnavailable, AgentStream, STREAM_WINDOW, StreamChunk
from app.agents.transfers import CHUNK_BYTES, upload_transfer, download_chunks
from app.config import settings
from app.worker_agent import WorkerAgent
from app.worker_transfers import WorkerTransfers
from tests.conftest import InProcessWorkerConnection, TEST_WORKER_ID
from app.agents.manager import agent_manager
from tests.test_workspaces import get_authenticated_headers


@pytest.mark.asyncio
async def test_large_file_roundtrip_is_chunked(client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_ROOT", str(tmp_path))
    headers = await get_authenticated_headers(client, "large_file")
    response = await client.post("/api/workspaces", headers=headers, json={
        "name": "large files", "template_id": "vscode-empty", "flavor_id": "t1.nano"})
    ws = response.json()
    connection = agent_manager.get(TEST_WORKER_ID)
    original = connection.request
    encoded_sizes = []
    async def observe(action, payload, timeout=60):
        if action == "transfer.write":
            encoded_sizes.append(len(payload["data"]))
        return await original(action, payload, timeout)
    monkeypatch.setattr(connection, "request", observe)
    content = b"0123456789abcdef" * (1024 * 1024) + b"tail"
    response = await client.post(f"/api/workspaces/{ws['id']}/files/upload", headers=headers,
                                 files={"files": ("large.bin", io.BytesIO(content))})
    assert response.status_code == 200, response.text
    downloaded = await client.get(f"/api/workspaces/{ws['id']}/files/download?path=large.bin", headers=headers)
    assert downloaded.status_code == 200
    assert hashlib.sha256(downloaded.content).digest() == hashlib.sha256(content).digest()
    assert len(encoded_sizes) > 64
    assert max(encoded_sizes) <= 4 * ((CHUNK_BYTES + 2) // 3)
    assert not connection.agent.transfers.items


@pytest.mark.asyncio
async def test_upload_limit_and_cancellation_preserve_existing_file(db_session, tmp_path, monkeypatch):
    agent = WorkerAgent()
    agent.registry = {"test": {"workspace_id": "id", "storage_path": str(tmp_path)}}
    connection = InProcessWorkerConnection(agent, db_session)
    target = tmp_path / "file.txt"
    target.write_bytes(b"original")
    async def too_large():
        yield b"x" * 20
    with pytest.raises(ValueError, match="limit"):
        await upload_transfer(connection, too_large(), payload={"purpose": "upload", "container_name": "test", "path": "file.txt"}, limit=10)
    assert target.read_bytes() == b"original"
    assert not agent.transfers.items
    written = asyncio.Event()
    async def interrupted():
        yield b"partial"
        written.set()
        await asyncio.Event().wait()
    task = asyncio.create_task(upload_transfer(connection, interrupted(), payload={"purpose": "upload", "container_name": "test", "path": "file.txt"}, limit=100))
    await asyncio.wait_for(written.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert target.read_bytes() == b"original"
    assert not agent.transfers.items
    await connection.cleanup()


@pytest.mark.asyncio
async def test_download_cancellation_closes_worker_handle(db_session, tmp_path):
    agent = WorkerAgent()
    agent.registry = {"test": {"workspace_id": "id", "storage_path": str(tmp_path)}}
    (tmp_path / "data").write_bytes(b"a" * (CHUNK_BYTES * 3))
    connection = InProcessWorkerConnection(agent, db_session)
    metadata = await connection.request("transfer.open", {"transfer_id": "read", "purpose": "download", "container_name": "test", "path": "data"})
    chunks = download_chunks(connection, "read", metadata["size"])
    assert len(await anext(chunks)) == CHUNK_BYTES
    await chunks.aclose()
    assert not agent.transfers.items
    await connection.cleanup()


@pytest.mark.asyncio
async def test_slow_stream_has_bounded_window_and_does_not_block_commands(db_session):
    agent = WorkerAgent()
    connection = InProcessWorkerConnection(agent, db_session)
    cancelled = asyncio.Event()
    async def produce(request_id, payload):
        try:
            await agent.result(request_id, {"status_code": 200})
            for _ in range(20):
                await agent.send({"type": "stream_data", "stream_id": payload["stream_id"], "encoding": "base64", "data": base64.b64encode(b"x" * CHUNK_BYTES).decode()})
            await agent.send({"type": "stream_end", "stream_id": payload["stream_id"]})
        finally:
            cancelled.set()
    agent.handle_http_open = produce
    try:
        _, stream = await connection.open_stream("proxy.http.open", {})
        # A control roundtrip is a deterministic scheduling point while the
        # producer waits for credits from its slow consumer.
        result = await asyncio.wait_for(connection.request("transfer.close", {"transfer_id": "absent"}), 2)
        assert result["closed"]
        assert stream.queue.qsize() == STREAM_WINDOW
        assert not cancelled.is_set()
        assert (await connection.receive_stream(stream)).data == b"x" * CHUNK_BYTES
        await connection.close_stream(stream.id)
        await asyncio.wait_for(cancelled.wait(), 2)
        assert not agent.stream_tasks
        assert not connection._streams
    finally:
        await connection.cleanup()


@pytest.mark.asyncio
async def test_noncompliant_worker_overflow_only_fails_its_stream():
    socket = type("Socket", (), {"send_json": AsyncMock()})()
    connection = AgentConnection("worker", socket)
    stream = AgentStream("overflow")
    other = AgentStream("other")
    connection._streams.update({stream.id: stream, other.id: other})
    for _ in range(STREAM_WINDOW + 1):
        await connection.handle_message({"type": "stream_data", "stream_id": stream.id, "encoding": "text", "data": "x"})
    assert stream.id not in connection._streams
    assert other.id in connection._streams
    assert isinstance(await stream.queue.get(), AgentCommandError)
    assert await stream.queue.get() is None
    await connection.disconnect()
    assert isinstance(await other.queue.get(), AgentUnavailable)


def test_worker_transfer_limits_and_expiry(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "WORKER_MAX_TRANSFERS", 1)
    monkeypatch.setattr(settings, "FILE_TRANSFER_MAX_BYTES", 10)
    store = WorkerTransfers(lambda name, path: tmp_path / path)
    store.command("transfer.open", {"transfer_id": "one", "purpose": "upload", "container_name": "test", "path": "file"})
    with pytest.raises(RuntimeError, match="limit"):
        store.command("transfer.open", {"transfer_id": "two", "purpose": "upload", "container_name": "test", "path": "file2"})
    with pytest.raises(ValueError, match="limit"):
        store.command("transfer.write", {"transfer_id": "one", "offset": 0, "data": base64.b64encode(b"x" * 11).decode()})
    handle = store.items["one"]["handle"]
    store.items["one"]["touched"] -= 121
    store.expire()
    assert handle.closed
    assert not store.items
    assert not (tmp_path / "file").exists()


@pytest.mark.asyncio
async def test_proxy_body_larger_than_old_message_limit_is_spooled_in_chunks(client, tmp_path, monkeypatch):
    import httpx
    import app.worker_agent as worker_module
    monkeypatch.setattr(settings, "STORAGE_ROOT", str(tmp_path))
    headers = await get_authenticated_headers(client, "large_proxy")
    ws = (await client.post("/api/workspaces", headers=headers, json={
        "name": "Proxy body", "template_id": "vscode-empty", "flavor_id": "t1.nano"})).json()
    sizes = []
    digest = hashlib.sha256()
    class Response:
        status_code = 200
        headers = httpx.Headers({"content-type": "text/plain"})
        async def aiter_raw(self):
            yield b"ok"
        async def aclose(self):
            pass
    class Upstream:
        def __init__(self, **kwargs):
            pass
        def build_request(self, **kwargs):
            return httpx.Request(**kwargs)
        async def send(self, request, stream):
            async for chunk in request.stream:
                sizes.append(len(chunk))
                digest.update(chunk)
            return Response()
        async def aclose(self):
            pass
    monkeypatch.setattr(worker_module.httpx, "AsyncClient", Upstream)
    body = b"proxy data" * (2 * 1024 * 1024)
    response = await client.post(f"/proxy/{ws['id']}/upload", headers=headers, content=body)
    assert response.status_code == 200, response.text
    assert response.text == "ok"
    assert digest.digest() == hashlib.sha256(body).digest()
    assert max(sizes) <= CHUNK_BYTES
    assert not agent_manager.get(TEST_WORKER_ID).agent.transfers.items
    monkeypatch.setattr(settings, "PROXY_MAX_REQUEST_BYTES", 10)
    rejected = await client.post(f"/proxy/{ws['id']}/upload", headers=headers, content=b"x" * 11)
    assert rejected.status_code == 413
    assert not agent_manager.get(TEST_WORKER_ID).agent.transfers.items


@pytest.mark.asyncio
async def test_small_http_frame_is_immediate_and_disconnect_closes_upstream(db_session, monkeypatch):
    import httpx
    import app.worker_agent as worker_module
    agent = WorkerAgent()
    connection = InProcessWorkerConnection(agent, db_session)
    closed = asyncio.Event()
    class Response:
        status_code = 200
        headers = httpx.Headers({"content-type": "text/event-stream"})
        async def aiter_raw(self):
            yield b"data: hello\n\n"
            await asyncio.Event().wait()
        async def aclose(self):
            closed.set()
    class Client:
        def __init__(self, **kwargs):
            pass
        def build_request(self, **kwargs):
            return None
        async def send(self, request, stream):
            return Response()
        async def aclose(self):
            pass
    monkeypatch.setattr(worker_module.httpx, "AsyncClient", Client)
    monkeypatch.setattr(agent, "_target_url", AsyncMock(return_value="http://worker/"))
    _, stream = await connection.open_stream("proxy.http.open", {"method": "GET"})
    chunk = await asyncio.wait_for(connection.receive_stream(stream), 2)
    assert chunk.data == b"data: hello\n\n"
    await connection.close_stream(stream.id)
    await asyncio.wait_for(closed.wait(), 2)
    await connection.cleanup()
    assert not agent.stream_tasks


@pytest.mark.asyncio
async def test_command_timeout_also_bounds_a_stalled_send():
    class Socket:
        async def send_json(self, message):
            await asyncio.Event().wait()
    connection = AgentConnection("stalled", Socket())
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(connection.request("test", {}, timeout=0.01), 1)
    assert not connection._pending


@pytest.mark.asyncio
async def test_cancel_during_backup_build_stops_builder_and_removes_tempfile(db_session, tmp_path, monkeypatch):
    import app.orchestrator.backup_service as backup
    agent = WorkerAgent()
    agent.registry = {"test": {"workspace_id": "id", "storage_path": str(tmp_path)}}
    connection = InProcessWorkerConnection(agent, db_session)
    started = asyncio.Event()
    loop = asyncio.get_running_loop()
    archive_paths = []
    def build(storage, archive, *, cancelled, max_bytes):
        archive_paths.append(archive)
        loop.call_soon_threadsafe(started.set)
        if not cancelled.wait(3):
            raise AssertionError("Backup did not receive cancellation")
        raise backup.BackupCancelled("cancelled")
    monkeypatch.setattr(backup, "create_workspace_zip_backup", build)
    pending = asyncio.create_task(connection.open_stream("workspace.backup.open", {
        "container_name": "test", "workspace_id": "id"}))
    await asyncio.wait_for(started.wait(), 2)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    # Let the worker finish its cooperative builder cancellation before teardown.
    await asyncio.wait_for(asyncio.gather(*connection.tasks, return_exceptions=True), 2)
    assert archive_paths and not archive_paths[0].exists()
    assert not agent.stream_tasks
    await connection.cleanup()


def test_backup_has_explicit_size_limit(tmp_path):
    from app.orchestrator.backup_service import create_workspace_zip_backup, BackupCancelled
    source = tmp_path / "source"
    source.mkdir()
    (source / "file").write_bytes(b"a" * 11)
    with pytest.raises(BackupCancelled, match="limit"):
        create_workspace_zip_backup(source, tmp_path / "backup.zip", max_bytes=10)


@pytest.mark.asyncio
async def test_worker_rejects_unbounded_legacy_stream_protocol(monkeypatch):
    agent = WorkerAgent()
    result = AsyncMock()
    monkeypatch.setattr(agent, "result", result)
    await agent.handle_command({"request_id": "legacy", "action": "proxy.http.open", "payload": {"stream_id": "old"}})
    assert "protocol mismatch" in result.call_args.kwargs["error"]
    assert not agent.stream_tasks
