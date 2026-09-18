from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class OnboardingSettings(Base):
    """Singleton policy controlling the versioned onboarding tour."""

    __tablename__ = "onboarding_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    current_version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    enabled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class OnboardingProgress(Base):
    """Current versioned tour state for one user."""

    __tablename__ = "onboarding_progress"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    tour_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="not_started"
    )
    current_topic: Mapped[str] = mapped_column(
        String(64), nullable=False, default=""
    )
    current_step: Mapped[str] = mapped_column(
        String(32), nullable=False, default=""
    )
    topic_choices_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}"
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
