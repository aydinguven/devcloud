from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ModelContainerRegistrySettings(Base):
    """Singleton destination registry for generated model serving images."""

    __tablename__ = "model_container_registry_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    registry_url: Mapped[str] = mapped_column(
        String(1024), default="", nullable=False
    )
    username: Mapped[str] = mapped_column(
        String(255), default="", nullable=False
    )
    encrypted_password: Mapped[str] = mapped_column(
        Text, default="", nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
