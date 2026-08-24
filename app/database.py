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
        existing_columns.add(column_name)


async def init_db() -> None:
    """Initialize database schemas and create tables."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await ensure_user_quota_columns(conn)
