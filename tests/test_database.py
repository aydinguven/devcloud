import asyncio

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings
from app.database import (
    ensure_download_settings_columns,
    ensure_user_quota_columns,
    ensure_workspace_columns,
)


@pytest.mark.asyncio
async def test_existing_users_table_receives_quota_columns(tmp_path):
    database_path = (tmp_path / "legacy.db").as_posix()
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "CREATE TABLE users ("
                    "id INTEGER PRIMARY KEY, "
                    "username VARCHAR(64) NOT NULL"
                    ")"
                )
            )
            await conn.execute(
                text("INSERT INTO users (id, username) VALUES (1, 'legacy')")
            )

        async def run_migration():
            async with engine.begin() as migration_conn:
                await ensure_user_quota_columns(migration_conn)

        await asyncio.gather(run_migration(), run_migration())

        async with engine.connect() as conn:
            columns = await conn.run_sync(
                lambda sync_conn: {
                    column["name"]
                    for column in inspect(sync_conn).get_columns("users")
                }
            )
            row = (
                await conn.execute(
                    text(
                        "SELECT cpu_quota, memory_mb_quota, disk_mb_quota "
                        "FROM users WHERE id = 1"
                    )
                )
            ).one()
    finally:
        await engine.dispose()

    assert {"cpu_quota", "memory_mb_quota", "disk_mb_quota"} <= columns
    assert row == (
        settings.DEFAULT_USER_CPU_QUOTA,
        settings.DEFAULT_USER_MEMORY_MB_QUOTA,
        settings.DEFAULT_USER_DISK_MB_QUOTA,
    )


@pytest.mark.asyncio
async def test_existing_workspaces_table_receives_node_id_column(tmp_path):
    database_path = (tmp_path / "legacy-workspaces.db").as_posix()
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE workspaces (id VARCHAR(36) PRIMARY KEY)"))
            await ensure_workspace_columns(conn)
            columns = await conn.run_sync(
                lambda sync_conn: {column["name"] for column in inspect(sync_conn).get_columns("workspaces")}
            )
    finally:
        await engine.dispose()
    assert "node_id" in columns


@pytest.mark.asyncio
async def test_existing_download_settings_table_receives_https_columns(tmp_path):
    database_path = (tmp_path / "legacy-download-settings.db").as_posix()
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "CREATE TABLE download_settings ("
                    "id INTEGER PRIMARY KEY, "
                    "public_base_url VARCHAR(1024) NOT NULL"
                    ")"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO download_settings (id, public_base_url) "
                    "VALUES (1, 'http://10.253.6.189')"
                )
            )
            await ensure_download_settings_columns(conn)
            columns = await conn.run_sync(
                lambda sync_conn: {
                    column["name"]
                    for column in inspect(sync_conn).get_columns("download_settings")
                }
            )
            row = (
                await conn.execute(
                    text(
                        "SELECT https_enabled, https_hostname, "
                        "http_fallback_enabled FROM download_settings WHERE id = 1"
                    )
                )
            ).one()
    finally:
        await engine.dispose()

    assert {
        "https_enabled",
        "https_hostname",
        "http_fallback_enabled",
        "certificate_subject",
        "certificate_not_after",
        "certificate_sha256",
    } <= columns
    assert row == (0, settings.HTTPS_DEFAULT_HOSTNAME, 1)
