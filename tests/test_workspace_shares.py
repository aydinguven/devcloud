import asyncio
import time
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi.responses import JSONResponse
from starlette.requests import Request

from app.models.workspace import Workspace, WorkspaceStatus
from app.models.workspace_share import WorkspaceShare
from app.proxy import router as proxy
from app.shares import COOKIE_PREFIX, cookie_name


@pytest_asyncio.fixture
async def shared_setup(client, db_session, monkeypatch):
    registration = await client.post('/api/auth/register', json={
        'username': 'share_owner', 'email': 'share@example.com', 'password': 'Password123!',
    })
    headers = {'Authorization': f"Bearer {registration.json()['access_token']}"}
    result = await client.post('/api/workspaces', headers=headers, json={
        'name': 'Shared app', 'template_id': 'vscode-empty', 'flavor_id': 't1.nano',
    })
    assert result.status_code == 201, result.text
    workspace_id = result.json()['id']
    workspace = await db_session.get(Workspace, workspace_id)
    workspace.status = WorkspaceStatus.RUNNING
    await db_session.commit()

    async def upstream(workspace, request, path, custom_port=None):
        return JSONResponse({'path': path, 'port': custom_port})

    monkeypatch.setattr(proxy, 'proxy_remote_http', upstream)
    return workspace_id, headers


@pytest.mark.asyncio
async def test_share_scope_revocation_and_token_separation(client, db_session, shared_setup):
    workspace_id, owner = shared_setup
    api = f'/api/workspaces/{workspace_id}/shares'
    page = await client.get(f'/workspaces/{workspace_id}', headers=owner)
    assert page.status_code == 200
    assert 'id="workspace-share-form"' in page.text
    result = await client.post(api, headers=owner, json={'port': 3333})
    assert result.status_code == 201
    share = result.json()
    assert share['expires_at'] is None
    assert not share['password_protected']
    client.cookies.clear()
    assert (await client.post(api, json={'port': 3333})).status_code == 401
    opened = await client.get(share['url'])
    assert opened.status_code == 303
    assert opened.headers['location'] == f'/proxy/{workspace_id}/port/3333/'
    assert 'HttpOnly' in opened.headers['set-cookie']
    assert (await client.get(opened.headers['location'] + 'nested/page')).json() == {'path': '/nested/page', 'port': 3333}
    for path in [f'/proxy/{workspace_id}/', f'/proxy/{workspace_id}/_devcloud/status',
                 f'/proxy/{workspace_id}/port/3334/', api, '/api/auth/me']:
        assert (await client.get(path)).status_code in (401, 403)
    # Even replaying the grant by hand cannot widen its scope.
    grant = next(value for name, value in client.cookies.items() if name.startswith(COOKIE_PREFIX))
    assert (await client.get(f'/proxy/{workspace_id}/port/3334/', headers={
        'Cookie': f'{cookie_name(workspace_id, 3334)}={grant}',
    })).status_code == 403
    assert (await client.get('/api/auth/me', headers={'Authorization': f'Bearer {grant}'})).status_code == 401
    assert (await client.get('/share/' + grant)).status_code == 404
    assert (await client.get(share['url'] + 'tampered')).status_code == 404
    assert (await client.delete(api + '/' + share['id'], headers=owner)).status_code == 204
    assert (await client.get(opened.headers['location'])).status_code == 404
    assert (await client.get(share['url'])).status_code == 404


@pytest.mark.asyncio
async def test_password_expiry_and_management_permissions(client, db_session, shared_setup):
    workspace_id, owner = shared_setup
    api = f'/api/workspaces/{workspace_id}/shares'
    for bad in ({'port': 0}, {'port': 65536}, {'port': 3333, 'expires_in_minutes': 0}):
        assert (await client.post(api, headers=owner, json=bad)).status_code == 422
    result = await client.post(api, headers=owner, json={'port': 3333, 'password': 'secret!', 'expires_in_minutes': 10})
    share = result.json()
    assert share['expires_at'] > time.time()
    assert 'password_hash' not in share
    record = await db_session.get(WorkspaceShare, share['id'])
    assert record.password_hash != 'secret!'
    client.cookies.clear()
    assert (await client.get(share['url'])).status_code == 200
    assert not client.cookies
    # A signed link itself is not an unlocked grant.
    token = share['url'].split('/')[-1]
    assert (await client.get(f'/proxy/{workspace_id}/port/3333/', headers={
        'Cookie': f'{cookie_name(workspace_id, 3333)}={token}',
    })).status_code == 404
    for _ in range(5):
        assert (await client.post(share['url'], data={'password': 'wrong'})).status_code == 401
    assert (await client.post(share['url'], data={'password': 'secret!'})).status_code == 429
    record.locked_until = 0
    await db_session.commit()
    unlocked = await client.post(share['url'], data={'password': 'secret!'})
    assert unlocked.status_code == 303
    assert (await client.get(unlocked.headers['location'])).status_code == 200
    record.expires_at = int(time.time()) - 1
    await db_session.commit()
    assert (await client.get(unlocked.headers['location'])).status_code == 404
    assert (await client.get(share['url'])).status_code == 404
    await client.post('/api/auth/register', json={
        'username': 'outsider', 'email': 'outsider@example.com', 'password': 'Password123!',
    })
    assert (await client.get(api)).status_code == 403
    assert (await client.post(api, json={'port': 3333})).status_code == 403
    assert (await client.delete(api + '/' + share['id'])).status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("invalidated_by", ["revoke", "expire"])
async def test_shared_websocket_revocation(client, db_session, shared_setup, monkeypatch, invalidated_by):
    workspace_id, owner = shared_setup
    api = f'/api/workspaces/{workspace_id}/shares'
    share = (await client.post(api, headers=owner, json={'port': 3333})).json()
    client.cookies.clear()
    await client.get(share['url'])
    connected = asyncio.Event()
    closed = []

    async def accept():
        pass

    async def close(**kwargs):
        closed.append(kwargs)

    async def remote(websocket, workspace, path, custom_port=None):
        assert custom_port == 3333
        assert path == '/socket'
        connected.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(proxy, 'proxy_remote_websocket', remote)
    socket = SimpleNamespace(accept=accept, close=close, query_params={}, cookies=dict(client.cookies))
    task = asyncio.create_task(proxy.proxy_websocket(socket, workspace_id, 'port/3333/socket', db_session))
    try:
        await asyncio.wait_for(connected.wait(), 2)
        record = await db_session.get(WorkspaceShare, share['id'])
        if invalidated_by == "revoke":
            record.revoked = True
        else:
            record.expires_at = int(time.time()) - 1
        await db_session.commit()
        await asyncio.wait_for(task, 4)
        assert closed[-1]['code'] != 1000
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_grant_cookie_is_never_forwarded():
    request = Request({'type': 'http', 'method': 'GET', 'path': '/', 'headers': [
        (b'cookie', b'devcloud_session=login; devcloud_share_ws_3333=grant; app_cookie=ok'),
    ]})
    headers = proxy._forward_headers(request, SimpleNamespace(template_id='vscode-empty'), 3333)
    assert headers['Cookie'] == 'app_cookie=ok'
