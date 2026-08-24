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
    assert await svc.container_exists(container_name) is True
    
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
    assert await svc.container_exists(container_name) is False


@pytest.mark.asyncio
async def test_jupyter_launch_uses_secure_workspace_base_url(monkeypatch):
    """Jupyter must remain token-protected and mounted below its proxy prefix."""
    svc = PodmanService(podman_bin="podman")
    svc._mock_mode = False
    commands = []

    async def fake_run_cmd(*args, timeout=None):
        commands.append(args)
        if args[0] == "run":
            return 0, "container-id", ""
        return 0, "", ""

    async def fake_ensure_image_exists(*args, **kwargs):
        return True

    class FakeWriter:
        def close(self):
            return None

        async def wait_closed(self):
            return None

    async def fake_open_connection(*args, **kwargs):
        return object(), FakeWriter()

    monkeypatch.setattr(
        svc, "ensure_workspace_storage", lambda user_id, workspace_id: "/workspace"
    )
    monkeypatch.setattr(svc, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(svc, "ensure_image_exists", fake_ensure_image_exists)
    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)

    workspace_id = "12345678-1234-1234-1234-123456789abc"
    await svc.create_workspace_container(
        workspace_id=workspace_id,
        user_id=1,
        container_name="devcloud-1-12345678",
        template_id="jupyter-python",
        flavor_id="t1.micro",
        host_port=10100,
        workspace_token="secret-workspace-token",
    )

    run_command = next(args for args in commands if args[0] == "run")
    image_index = run_command.index("localhost/devcloud-jupyter-python:latest")
    startup_command = run_command[image_index + 1:]

    assert startup_command[0] == "start-notebook.py"
    assert f"--ServerApp.base_url=/proxy/{workspace_id}/" in startup_command
    assert "--ServerApp.default_url=/lab" in startup_command
    assert "-e" in run_command
    assert "JUPYTER_TOKEN=secret-workspace-token" in run_command
    assert not any("disable_check_xsrf" in arg for arg in startup_command)
    assert not any("allow_origin" in arg for arg in startup_command)
