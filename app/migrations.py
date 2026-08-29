"""Small, explicit, database-portable schema migration runner.

The project historically altered tables during web-process startup.  Managed
installations now run this module before restarting the controller, which
keeps schema ownership out of Uvicorn and works with SQLite or PostgreSQL.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
from datetime import datetime, timezone

from sqlalchemy import inspect, text

from app.config import settings
from app.database import engine, init_db


CURRENT_SCHEMA_VERSION = 5


class MigrationError(RuntimeError):
    pass


def _rebuild_sqlite_workspaces(sync_conn) -> bool:
    """Replace a legacy SQLite workspace table with the worker-only schema."""
    from app.models.workspace import Workspace

    inspector = inspect(sync_conn)
    columns = inspector.get_columns("workspaces")
    node_column = next(column for column in columns if column["name"] == "node_id")
    unique_sets = {
        tuple(constraint.get("column_names") or ())
        for constraint in inspector.get_unique_constraints("workspaces")
    }
    unique_sets.update(
        tuple(index.get("column_names") or ())
        for index in inspector.get_indexes("workspaces")
        if index.get("unique")
    )
    if not node_column.get("nullable", True) and ("host_port",) not in unique_sets:
        return False

    legacy = "workspaces_legacy_v3"
    if legacy in inspector.get_table_names():
        raise MigrationError(
            f"Cannot rebuild workspaces while stale table {legacy!r} exists"
        )
    preparer = sync_conn.dialect.identifier_preparer
    sync_conn.exec_driver_sql(
        f"ALTER TABLE {preparer.quote('workspaces')} "
        f"RENAME TO {preparer.quote(legacy)}"
    )
    legacy_inspector = inspect(sync_conn)
    for index in legacy_inspector.get_indexes(legacy):
        name = str(index.get("name") or "")
        if name and not name.startswith("sqlite_autoindex"):
            sync_conn.exec_driver_sql(
                f"DROP INDEX {preparer.quote(name)}"
            )
    Workspace.__table__.create(sync_conn)
    old_columns = {
        column["name"] for column in legacy_inspector.get_columns(legacy)
    }
    common = [
        column.name for column in Workspace.__table__.columns
        if column.name in old_columns
    ]
    quoted = ", ".join(preparer.quote(name) for name in common)
    sync_conn.exec_driver_sql(
        f"INSERT INTO {preparer.quote('workspaces')} ({quoted}) "
        f"SELECT {quoted} FROM {preparer.quote(legacy)}"
    )
    sync_conn.exec_driver_sql(f"DROP TABLE {preparer.quote(legacy)}")
    return True


async def _record_version(conn, version: int, name: str) -> None:
    await conn.execute(
        text(
            "INSERT INTO devcloud_schema_migrations "
            "(version, name, applied_at) VALUES (:version, :name, :applied_at)"
        ),
        {
            "version": version,
            "name": name,
            "applied_at": datetime.now(timezone.utc).isoformat(),
        },
    )


async def _applied_versions(conn) -> set[int]:
    await conn.execute(
        text(
            "CREATE TABLE IF NOT EXISTS devcloud_schema_migrations ("
            "version INTEGER PRIMARY KEY, "
            "name VARCHAR(255) NOT NULL, "
            "applied_at VARCHAR(64) NOT NULL)"
        )
    )
    rows = await conn.execute(
        text("SELECT version FROM devcloud_schema_migrations")
    )
    return {int(row[0]) for row in rows}


async def _seed_bootstrap_worker(conn) -> None:
    worker_id = settings.DEVCLOUD_BOOTSTRAP_WORKER_ID.strip()
    token_hash = settings.DEVCLOUD_BOOTSTRAP_WORKER_TOKEN_HASH.strip()
    if not worker_id:
        return
    if len(token_hash) != hashlib.sha256().digest_size * 2:
        raise MigrationError("Bootstrap worker token hash is invalid")
    worker_name = (
        settings.DEVCLOUD_BOOTSTRAP_WORKER_NAME.strip() or "all-in-one-worker"
    )
    exists = await conn.execute(
        text("SELECT id FROM nodes WHERE id = :id"), {"id": worker_id}
    )
    if exists.first():
        await conn.execute(
            text(
                "UPDATE nodes SET name = :name, agent_token_hash = :token_hash, "
                "enabled = :enabled, schedulable = :schedulable WHERE id = :id"
            ),
            {
                "id": worker_id,
                "name": worker_name,
                "token_hash": token_hash,
                "enabled": True,
                "schedulable": True,
            },
        )
        return
    await conn.execute(
        text(
            "INSERT INTO nodes "
            "(id, name, hostname, enabled, schedulable, status, cpu_total, "
            "memory_total_mb, disk_total_mb, cpu_percent, memory_used_mb, "
            "disk_used_mb, active_containers_count, labels_json, capabilities_json, "
            "inventory_json, reconciliation_json, agent_version, agent_token_hash, "
            "created_at, updated_at) "
            "VALUES (:id, :name, '', :enabled, :schedulable, :status, 0, 0, 0, "
            "0, 0, 0, 0, '{}', '{}', '[]', '{}', '', :token_hash, :created_at, "
            ":updated_at)"
        ),
        {
            "id": worker_id,
            "name": worker_name,
            "enabled": True,
            "schedulable": True,
            "status": "PENDING",
            "token_hash": token_hash,
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        },
    )


async def _assign_legacy_workspaces(conn) -> None:
    count = int(
        (
            await conn.execute(
                text("SELECT COUNT(*) FROM workspaces WHERE node_id IS NULL")
            )
        ).scalar_one()
    )
    if not count:
        return
    rows = (
        await conn.execute(text("SELECT id FROM nodes ORDER BY created_at, id"))
    ).all()
    if len(rows) != 1:
        raise MigrationError(
            f"{count} legacy workspace(s) have no worker assignment. "
            "Register exactly one migration target worker, or assign node_id "
            "before upgrading."
        )
    await conn.execute(
        text("UPDATE workspaces SET node_id = :node_id WHERE node_id IS NULL"),
        {"node_id": rows[0][0]},
    )


async def _worker_only_constraints(conn) -> None:
    await _seed_bootstrap_worker(conn)
    await _assign_legacy_workspaces(conn)
    dialect = conn.dialect.name
    if dialect == "postgresql":
        constraints = await conn.run_sync(
            lambda sync_conn: inspect(sync_conn).get_unique_constraints(
                "workspaces"
            )
        )
        preparer = conn.dialect.identifier_preparer
        for constraint in constraints:
            if constraint.get("column_names") == ["host_port"] and constraint.get(
                "name"
            ):
                quoted = preparer.quote(constraint["name"])
                await conn.execute(
                    text(f"ALTER TABLE workspaces DROP CONSTRAINT {quoted}")
                )
        await conn.execute(
            text("ALTER TABLE workspaces ALTER COLUMN node_id SET NOT NULL")
        )
    elif dialect == "sqlite":
        await conn.run_sync(_rebuild_sqlite_workspaces)
    unique_sets = await conn.run_sync(
        lambda sync_conn: {
            tuple(item.get("column_names") or ())
            for item in (
                inspect(sync_conn).get_unique_constraints("workspaces")
                + [
                    index
                    for index in inspect(sync_conn).get_indexes("workspaces")
                    if index.get("unique")
                ]
            )
        }
    )
    if ("node_id", "host_port") not in unique_sets:
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_workspaces_node_host_port "
                "ON workspaces (node_id, host_port)"
            )
        )


async def upgrade() -> None:
    # The legacy initializer remains the compatibility migration for all
    # pre-versioned installations.
    await init_db()
    async with engine.begin() as conn:
        applied = await _applied_versions(conn)
        if 1 not in applied:
            await _record_version(conn, 1, "version existing schema")
        if 2 not in applied:
            # init_db adds all telemetry/inventory columns idempotently.
            await _record_version(conn, 2, "worker telemetry and inventory")
        if 3 not in applied:
            await _worker_only_constraints(conn)
            await _record_version(conn, 3, "worker-only workspace placement")
        if 4 not in applied:
            # init_db creates the portable workspace_images table.
            await _record_version(conn, 4, "controller-managed workspace images")
        if 5 not in applied:
            # init_db creates the portable worker_bootstrap_tickets table.
            await _record_version(conn, 5, "single-use worker bootstrap tickets")


async def current_version() -> int:
    async with engine.begin() as conn:
        tables = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names())
        if "devcloud_schema_migrations" not in tables:
            return 0
        value = (
            await conn.execute(
                text("SELECT MAX(version) FROM devcloud_schema_migrations")
            )
        ).scalar_one_or_none()
        return int(value or 0)


async def require_current() -> None:
    version = await current_version()
    if version != CURRENT_SCHEMA_VERSION:
        raise MigrationError(
            f"Database schema is version {version}; expected "
            f"{CURRENT_SCHEMA_VERSION}. Run 'python -m app.migrations upgrade'."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.migrations")
    parser.add_argument("command", choices=["upgrade", "current", "check"])
    args = parser.parse_args(argv)
    if args.command == "upgrade":
        asyncio.run(upgrade())
        return 0
    version = asyncio.run(current_version())
    if args.command == "current":
        print(version)
        return 0
    if version != CURRENT_SCHEMA_VERSION:
        print(f"schema version {version}; expected {CURRENT_SCHEMA_VERSION}")
        return 1
    print(f"schema version {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
