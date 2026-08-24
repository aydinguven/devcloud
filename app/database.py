from typing import AsyncGenerator
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


async def init_db() -> None:
    """Initialize database schemas and create tables."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
