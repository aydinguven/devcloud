import asyncio

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings
from app.database import ensure_user_quota_columns


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
