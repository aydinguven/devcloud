from typing import AsyncGenerator
from sqlalchemy import inspect, text
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

# Create async engine
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False} if "sqlite" in settings.DATABASE_URL else {},
)

# Async session factory
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy models."""
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency for providing database sessions to FastAPI routes."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


async def _user_column_names(conn) -> set[str]:
    return await conn.run_sync(
        lambda sync_conn: {
            column["name"] for column in inspect(sync_conn).get_columns("users")
        }
    )


async def ensure_user_quota_columns(conn) -> None:
    """Add quota columns safely, including during multi-worker startup."""
    existing_columns = await _user_column_names(conn)
    quota_columns = {
        "cpu_quota": (
            "FLOAT",
            settings.DEFAULT_USER_CPU_QUOTA,
        ),
        "memory_mb_quota": (
            "INTEGER",
            settings.DEFAULT_USER_MEMORY_MB_QUOTA,
        ),
        "disk_mb_quota": (
            "INTEGER",
            settings.DEFAULT_USER_DISK_MB_QUOTA,
        ),
    }
    for column_name, (column_type, default_value) in quota_columns.items():
        if column_name in existing_columns:
            continue
        try:
            async with conn.begin_nested():
                await conn.execute(
                    text(
                        f"ALTER TABLE users ADD COLUMN {column_name} {column_type} "
                        f"NOT NULL DEFAULT {default_value}"
                    )
                )
        except (OperationalError, ProgrammingError):
            # Another Uvicorn worker may have completed the same migration.
            current_columns = await _user_column_names(conn)
            if column_name not in current_columns:
                raise


async def _workspace_column_names(conn) -> set[str]:
    return await conn.run_sync(
        lambda sync_conn: {
            column["name"] for column in inspect(sync_conn).get_columns("workspaces")
        }
    )


async def ensure_workspace_columns(conn) -> None:
    """Add new workspace columns safely."""
    existing_columns = await _workspace_column_names(conn)
    if "auto_stop_minutes" not in existing_columns:
        try:
            async with conn.begin_nested():
                await conn.execute(
                    text("ALTER TABLE workspaces ADD COLUMN auto_stop_minutes INTEGER NOT NULL DEFAULT 0")
                )
        except (OperationalError, ProgrammingError):
            pass
    if "node_id" not in existing_columns:
        try:
            async with conn.begin_nested():
                await conn.execute(
                    text("ALTER TABLE workspaces ADD COLUMN node_id VARCHAR(36)")
                )
        except (OperationalError, ProgrammingError):
            current_columns = await _workspace_column_names(conn)
            if "node_id" not in current_columns:
                raise
    try:
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_workspaces_node_id ON workspaces (node_id)")
        )
    except (OperationalError, ProgrammingError):
        # Some managed databases use a different idempotent-index syntax; the
        # column remains functional and a real migration can add the index.
        pass


async def _download_settings_column_names(conn) -> set[str]:
    return await conn.run_sync(
        lambda sync_conn: {
            column["name"]
            for column in inspect(sync_conn).get_columns("download_settings")
        }
    )


async def ensure_download_settings_columns(conn) -> None:
    """Add HTTPS ingress settings to existing singleton settings tables."""
    existing_columns = await _download_settings_column_names(conn)
    default_hostname = settings.HTTPS_DEFAULT_HOSTNAME.replace("'", "''")
    columns = {
        "https_enabled": "BOOLEAN NOT NULL DEFAULT 0",
        "https_hostname": (
            "VARCHAR(253) NOT NULL DEFAULT "
            f"'{default_hostname}'"
        ),
        "http_fallback_enabled": "BOOLEAN NOT NULL DEFAULT 1",
        "certificate_subject": "VARCHAR(1024)",
        "certificate_not_after": "VARCHAR(64)",
        "certificate_sha256": "VARCHAR(64)",
    }
    for column_name, definition in columns.items():
        if column_name in existing_columns:
            continue
        try:
            async with conn.begin_nested():
                await conn.execute(
                    text(
                        f"ALTER TABLE download_settings ADD COLUMN "
                        f"{column_name} {definition}"
                    )
                )
        except (OperationalError, ProgrammingError):
            current_columns = await _download_settings_column_names(conn)
            if column_name not in current_columns:
                raise


async def _node_column_names(conn) -> set[str]:
    return await conn.run_sync(
        lambda sync_conn: {
            column["name"]
            for column in inspect(sync_conn).get_columns("nodes")
        }
    )


async def ensure_node_columns(conn) -> None:
    """Add telemetry and metadata columns to existing nodes tables."""
    try:
        existing_columns = await _node_column_names(conn)
    except (OperationalError, ProgrammingError):
        return

    columns = {
        "cpu_percent": "FLOAT NOT NULL DEFAULT 0.0",
        "memory_used_mb": "INTEGER NOT NULL DEFAULT 0",
        "disk_used_mb": "INTEGER NOT NULL DEFAULT 0",
        "active_containers_count": "INTEGER NOT NULL DEFAULT 0",
    }
    for column_name, definition in columns.items():
        if column_name in existing_columns:
            continue
        try:
            async with conn.begin_nested():
                await conn.execute(
                    text(f"ALTER TABLE nodes ADD COLUMN {column_name} {definition}")
                )
        except (OperationalError, ProgrammingError):
            current_columns = await _node_column_names(conn)
            if column_name not in current_columns:
                raise


async def init_db() -> None:
    """Initialize database schemas and create tables."""
    # Ensure models are imported so Base has metadata
    from app.models.user import User  # noqa: F401
    from app.models.workspace import Workspace  # noqa: F401
    from app.models.custom_template import CustomTemplate  # noqa: F401
    from app.models.directory_settings import DirectorySettings  # noqa: F401
    from app.models.node import Node  # noqa: F401
    from app.models.mlflow_settings import MlflowSettings  # noqa: F401
    from app.models.download_settings import DownloadSettings  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await ensure_user_quota_columns(conn)
        await ensure_workspace_columns(conn)
        await ensure_download_settings_columns(conn)
        await ensure_node_columns(conn)
