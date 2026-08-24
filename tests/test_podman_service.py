import asyncio

import pytest
from app.orchestrator.flavors import get_flavor
from app.orchestrator.templates import get_template
from app.orchestrator.podman_service import PodmanService


@pytest.mark.asyncio
async def test_run_cmd_waits_for_cli_process_without_communicate(monkeypatch):
    """Detached container descendants must not keep command capture open."""
    captured = {}

    class FakeProcess:
        returncode = 0

        async def wait(self):
            return self.returncode

        async def communicate(self):
            raise AssertionError("run_cmd must not wait for pipe EOF via communicate()")

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured.update(kwargs)
        kwargs["stdout"].write(b"container-id\n")
        kwargs["stderr"].write(b"")
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    svc = PodmanService(podman_bin="podman")

    code, stdout, stderr = await svc.run_cmd("run", "-d", "example")

    assert captured["stdout"] != asyncio.subprocess.PIPE
    assert captured["stderr"] != asyncio.subprocess.PIPE
    assert (code, stdout, stderr) == (0, "container-id", "")

@pytest.mark.asyncio
async def test_podman_service_mock_lifecycle():
    """Test Podman service creation, execution and state manipulation in mock mode."""
    svc = PodmanService()
    svc._mock_mode = True

    container_name = "devcloud-1-test-ws-1234"
    # 1. Create container
    cid, storage_path = await svc.create_workspace_container(
        workspace_id="test-ws-12345",
        user_id=1,
        container_name=container_name,
        template_id="vscode-java",
        flavor_id="t1.mini",
        host_port=10105,
        workspace_token="testtoken123",
    )
    assert cid.startswith("mock-cid-")
    assert "test-ws-12345" in storage_path
    
    # 2. Check status
    status = await svc.get_container_status(container_name)
    assert status == "running"

    # 3. Stop
    assert await svc.stop_container(container_name) is True
    assert await svc.get_container_status(container_name) == "stopped"

    # 4. Start
    assert await svc.start_container(container_name) is True
    assert await svc.get_container_status(container_name) == "running"

    # 5. Retrieve logs
    logs = await svc.get_logs(container_name)
    assert "Allocated 2.0 CPU" in logs

    # 6. Delete
    assert await svc.delete_container(container_name) is True
