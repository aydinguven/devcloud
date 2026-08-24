import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.models.user import User
from app.models.workspace import Workspace, WorkspaceStatus
from app.orchestrator.flavors import get_flavor
from app.orchestrator.podman_service import podman_service

async def get_authenticated_headers(client: AsyncClient, username: str = "dev_user") -> dict[str, str]:
    """Helper to register and return authorization headers."""
    resp = await client.post(
        "/api/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "Password123!",
            "full_name": "Dev User",
        },
    )
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_template_and_flavor_catalogs(client: AsyncClient):
    """Test retrieving template and flavor definitions."""
    tpl_resp = await client.get("/api/workspaces/templates")
    assert tpl_resp.status_code == 200
    templates = tpl_resp.json()
    tpl_ids = [t["id"] for t in templates]
    template_names = {t["id"]: t["name"] for t in templates}
    assert "vscode-empty" in tpl_ids
    assert "vscode-python" in tpl_ids
    assert "vscode-react" in tpl_ids
    assert "jupyter-python" in tpl_ids
    assert "vscode-java" in tpl_ids
    for tid, tname in {
        "vscode-empty": "Boş Proje",
        "vscode-python": "Python 3.14",
        "vscode-react": "React/Node.js",
        "jupyter-python": "Jupyter Notebook",
        "vscode-java": "Java 21 LTS",
    }.items():
        assert template_names.get(tid) == tname

    flavor_resp = await client.get("/api/workspaces/flavors")
    assert flavor_resp.status_code == 200
    flavors = flavor_resp.json()
    flavor_ids = [f["id"] for f in flavors]
    assert flavor_ids == [
        "t1.nano",
        "t1.micro",
        "t1.small",
        "t1.medium",
        "t1.large",
        "t1.xlarge",
    ]
    flavor_resources = {
        flavor["id"]: (flavor["cpus"], flavor["memory_mb"])
        for flavor in flavors
    }
    assert flavor_resources == {
        "t1.nano": (0.5, 512),
        "t1.micro": (1.0, 1024),
        "t1.small": (1.0, 2048),
        "t1.medium": (2.0, 4096),
        "t1.large": (4.0, 8192),
        "t1.xlarge": (8.0, 16384),
    }

    legacy_mini = get_flavor("t1.mini")
    assert legacy_mini is not None
    assert legacy_mini.selectable is False


@pytest.mark.asyncio
async def test_legacy_flavor_cannot_be_used_for_new_workspace(client: AsyncClient):
    """Hidden legacy flavors remain restartable but cannot be newly deployed."""
    headers = await get_authenticated_headers(client, "legacy_flavor_tester")
    payload = {
        "name": "Legacy Flavor",
        "description": "",
        "template_id": "vscode-empty",
        "flavor_id": "t1.mini",
    }

    response = await client.post("/api/workspaces", json=payload, headers=headers)

    assert response.status_code == 400
    assert response.json()["detail"] == "Geçersiz kaynak profili ID: t1.mini"

    streamed = await client.post(
        "/api/workspaces/deploy-stream", json=payload, headers=headers
    )
    assert streamed.status_code == 200
    assert "Geçersiz kaynak profili: t1.mini" in streamed.text
    assert '"type": "done"' not in streamed.text


@pytest.mark.asyncio
async def test_workspace_lifecycle(client: AsyncClient):
    """Test creating, starting, stopping, getting logs, and deleting workspaces."""
    headers = await get_authenticated_headers(client, "workspace_tester")

    # 1. Create Python Workspace with t1.micro
    create_payload = {
        "name": "Python Analytics",
        "description": "My data project",
        "template_id": "vscode-python",
        "flavor_id": "t1.micro",
    }
    create_resp = await client.post("/api/workspaces", json=create_payload, headers=headers)
    assert create_resp.status_code == 201, create_resp.text
    ws = create_resp.json()
    ws_id = ws["id"]
    assert ws["name"] == "Python Analytics"
    assert ws["status"] == "running"
    assert ws["template_id"] == "vscode-python"
    assert ws["flavor_id"] == "t1.micro"
    assert ws["host_port"] >= 10100

    # 2. List Workspaces
    list_resp = await client.get("/api/workspaces", headers=headers)
    assert list_resp.status_code == 200
    assert len(list_resp.json()) == 1

    # 3. Get Logs
    logs_resp = await client.get(f"/api/workspaces/{ws_id}/logs", headers=headers)
    assert logs_resp.status_code == 200
    assert "başlatılıyor" in logs_resp.json()["logs"]

    # 4. Stop Workspace
    stop_resp = await client.post(f"/api/workspaces/{ws_id}/stop", headers=headers)
    assert stop_resp.status_code == 200
    assert stop_resp.json()["status"] == "stopped"

    # 5. Start Workspace
    start_resp = await client.post(f"/api/workspaces/{ws_id}/start", headers=headers)
    assert start_resp.status_code == 200
    assert start_resp.json()["status"] == "running"

    # 6. Delete Workspace
    del_resp = await client.delete(f"/api/workspaces/{ws_id}", headers=headers)
    assert del_resp.status_code == 200

    # Verify deleted
    get_resp = await client.get(f"/api/workspaces/{ws_id}", headers=headers)
    assert get_resp.status_code == 404


@pytest.mark.asyncio
async def test_start_recreates_missing_container_with_same_workspace(client: AsyncClient):
    """A removed container is recreated without deleting its workspace record or storage."""
    headers = await get_authenticated_headers(client, "recreate_workspace_tester")
    create_resp = await client.post(
        "/api/workspaces",
        json={
            "name": "Recreate Me",
            "description": "Persistent data stays mounted",
            "template_id": "jupyter-python",
            "flavor_id": "t1.micro",
        },
        headers=headers,
    )
    assert create_resp.status_code == 201
    workspace = create_resp.json()

    # Simulate out-of-band removal while the database still says "running".
    assert await podman_service.delete_container(workspace["container_name"]) is True
    assert await podman_service.container_exists(workspace["container_name"]) is False

    start_resp = await client.post(
        f"/api/workspaces/{workspace['id']}/start", headers=headers
    )

    assert start_resp.status_code == 200
    assert start_resp.json()["status"] == "running"
    assert start_resp.json()["storage_path"] == workspace["storage_path"]
    assert await podman_service.container_exists(workspace["container_name"]) is True


@pytest.mark.asyncio
async def test_deploy_workspace_stream(client: AsyncClient):
    """Test creating a workspace with the streaming SSE log endpoint."""
    headers = await get_authenticated_headers(client, "stream_tester")

    payload = {
        "name": "Streamed App",
        "description": "SSE test project",
        "template_id": "jupyter-python",
        "flavor_id": "t1.nano",
    }
    resp = await client.post("/api/workspaces/deploy-stream", json=payload, headers=headers)
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers.get("content-type", "")
    body = resp.text
    assert "data: " in body
    assert "kurulum süreci başlatılıyor" in body
    assert "done" in body



@pytest.mark.asyncio
async def test_delete_rejects_workspace_during_deployment(client: AsyncClient, db_session):
    """Deleting a creating workspace must not race its deployment task."""
    headers = await get_authenticated_headers(client, "creating_workspace_tester")
    result = await db_session.execute(
        select(User).where(User.username == "creating_workspace_tester")
    )
    user = result.scalar_one()

    workspace = Workspace(
        name="Still Deploying",
        description="",
        user_id=user.id,
        template_id="vscode-python",
        flavor_id="t1.micro",
        container_name="devcloud-creating-test",
        host_port=11999,
        container_port=8080,
        storage_path="",
        status=WorkspaceStatus.CREATING,
    )
    db_session.add(workspace)
    await db_session.commit()
    await db_session.refresh(workspace)

    response = await client.delete(
        f"/api/workspaces/{workspace.id}",
        headers=headers,
    )

    assert response.status_code == 409
    assert "kurulumu devam ediyor" in response.json()["detail"]
    assert await db_session.get(Workspace, workspace.id) is not None


@pytest.mark.asyncio
async def test_workspace_creation_enforces_user_cpu_and_ram_quota(
    client: AsyncClient,
    db_session,
):
    headers = await get_authenticated_headers(client, "quota_workspace_tester")
    result = await db_session.execute(
        select(User).where(User.username == "quota_workspace_tester")
    )
    user = result.scalar_one()
    user.cpu_quota = 0.5
    user.memory_mb_quota = 512
    user.disk_mb_quota = 1024
    db_session.add(user)
    await db_session.commit()

    payload = {
        "name": "Within Quota",
        "description": "",
        "template_id": "vscode-empty",
        "flavor_id": "t1.nano",
    }
    first = await client.post("/api/workspaces", json=payload, headers=headers)
    assert first.status_code == 201

    second = await client.post(
        "/api/workspaces",
        json={**payload, "name": "Over Quota"},
        headers=headers,
    )
    assert second.status_code == 409
    assert "Kullanıcı kotası aşıldı" in second.json()["detail"]
    assert "CPU 1.0/0.5 olacak" in second.json()["detail"]
    assert "RAM 1024/512 MB olacak" in second.json()["detail"]

    streamed = await client.post(
        "/api/workspaces/deploy-stream",
        json={**payload, "name": "Stream Over Quota"},
        headers=headers,
    )
    assert streamed.status_code == 200
    assert "Kullanıcı kotası aşıldı" in streamed.text
    assert '"type": "done"' not in streamed.text
