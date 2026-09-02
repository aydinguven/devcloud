from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class FlavorSettings(Base):
    """Admin-managed built-in overrides and custom resource flavors."""

    __tablename__ = "flavor_settings"

    flavor_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_custom: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    description: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    cpus: Mapped[float | None] = mapped_column(Float, nullable=True)
    memory_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    accelerator_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    accelerator_vendor: Mapped[str] = mapped_column(String(50), default="", nullable=False)
    accelerator_memory_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
