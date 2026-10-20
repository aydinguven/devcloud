from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class DeployRegistrySettings(Base):
    """Singleton deploy registry (Nexus) that Kubernetes image releases go to."""

    __tablename__ = "deploy_registry_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    registry_url: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    username: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    encrypted_password: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # False lets skopeo skip certificate checks (and fall back to plain HTTP).
    tls_verify: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="1", nullable=False
    )
    # "" copies the manifest unchanged; "v2s2" converts for registries without OCI.
    manifest_format: Mapped[str] = mapped_column(
        String(16), default="", server_default="", nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
