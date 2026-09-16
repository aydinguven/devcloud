from datetime import datetime, timezone

import pytest

from app.auth import decode_access_token
from app.config import Settings, settings
from app.models.session_settings import SessionSettings
from app.models.user import User, UserRole


def assert_lifetime(response, minutes, started):
    assert response.status_code in (200, 201), response.text
    payload = decode_access_token(response.json()["access_token"])
    assert started + minutes * 60 - 1 <= payload["exp"] <= datetime.now(timezone.utc).timestamp() + minutes * 60
    assert f"Max-Age={minutes * 60}" in response.headers["set-cookie"]
    return payload


@pytest.mark.asyncio
async def test_session_policy_and_expiration(client, db_session, monkeypatch):
    assert Settings(_env_file=None).ACCESS_TOKEN_EXPIRE_MINUTES == 240
    monkeypatch.setattr(settings, "ACCESS_TOKEN_EXPIRE_MINUTES", 240)
    endpoint = "/api/admin/session-settings"
    assert (await client.get(endpoint)).status_code == 401
    assert (await client.put(endpoint, json={"timeout_minutes": 30})).status_code == 401

    credentials = {"username": "session_admin", "password": "Password123!"}
    started = datetime.now(timezone.utc).timestamp()
    registration = await client.post("/api/auth/register", json={
        **credentials, "email": "session-admin@example.com",
    })
    original = assert_lifetime(registration, 240, started)
    assert (await client.get(endpoint)).status_code == 403
    assert (await client.put(endpoint, json={"timeout_minutes": 30})).status_code == 403

    user = await db_session.get(User, registration.json()["user"]["id"])
    user.role = UserRole.ADMIN
    await db_session.commit()
    assert (await client.get(endpoint)).json() == {"timeout_minutes": 240}
    for invalid in (0, -1, 10081, 1.5, True, "60", None):
        response = await client.put(endpoint, json={"timeout_minutes": invalid})
        assert response.status_code == 422

    updated = await client.put(endpoint, json={"timeout_minutes": 30})
    assert updated.status_code == 200
    assert (await client.get(endpoint)).json() == {"timeout_minutes": 30}
    db_session.expire_all()
    assert (await db_session.get(SessionSettings, 1)).timeout_minutes == 30
    page = await client.get("/admin/system")
    assert page.status_code == 200
    assert 'id="session-settings-form"' in page.text
    assert 'value="30"' in page.text

    started = datetime.now(timezone.utc).timestamp()
    login = await client.post("/api/auth/login", json=credentials)
    payload = assert_lifetime(login, 30, started)
    assert decode_access_token(registration.json()["access_token"])["exp"] == original["exp"]
    assert (await client.put(endpoint, json={"timeout_minutes": 60})).status_code == 200
    started = datetime.now(timezone.utc).timestamp()
    new_user = await client.post("/api/auth/register", json={
        "username": "new_session", "password": "Password123!", "email": "new-session@example.com",
    })
    assert_lifetime(new_user, 60, started)

    # Exercise the actual authentication dependency for both cookie and bearer expiry.
    class ExpiredClock:
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(payload["exp"] + 1, timezone.utc)

    monkeypatch.setattr("jwt.api_jwt.datetime", ExpiredClock)
    token = login.json()["access_token"]
    client.cookies.clear()
    assert (await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})).status_code == 401
    client.cookies.set(settings.COOKIE_NAME, token)
    assert (await client.get("/api/auth/me")).status_code == 401
