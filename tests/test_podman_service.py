import pytest
from app.orchestrator.flavors import get_flavor
from app.orchestrator.templates import get_template
from app.orchestrator.podman_service import PodmanService


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
