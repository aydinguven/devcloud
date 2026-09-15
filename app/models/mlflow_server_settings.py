from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class MlflowServerSettings(Base):
    """Singleton admin-managed MLflow server and TLS policy."""

    __tablename__ = "mlflow_server_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    base_url: Mapped[str] = mapped_column(String(1024), default="", nullable=False)
    validate_tls: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    ca_cert_file: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
