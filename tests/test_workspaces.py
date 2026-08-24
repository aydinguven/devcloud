import pytest
from httpx import AsyncClient


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
    assert "vscode-empty" in tpl_ids
    assert "vscode-python" in tpl_ids
    assert "jupyter-python" in tpl_ids
    assert "vscode-java" in tpl_ids

    flavor_resp = await client.get("/api/workspaces/flavors")
    assert flavor_resp.status_code == 200
    flavors = flavor_resp.json()
    flavor_ids = [f["id"] for f in flavors]
    assert "t1.nano" in flavor_ids
    assert "t1.micro" in flavor_ids
    assert "t1.mini" in flavor_ids


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
    assert "Initializing" in logs_resp.json()["logs"]

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
    assert "Initializing deployment" in body
    assert "done" in body

