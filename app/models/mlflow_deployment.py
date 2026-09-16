import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class MlflowModelBuildStatus(str, enum.Enum):
    QUEUED = "queued"
    VALIDATING = "validating"
    BUILDING = "building"
    IMPORTING = "importing"
    READY = "ready"
    FAILED = "failed"
    DELETING = "deleting"


class MlflowDeploymentStatus(str, enum.Enum):
    QUEUED = "queued"
    WAITING_FOR_IMAGE = "waiting_for_image"
    SCHEDULING = "scheduling"
    STARTING = "starting"
    HEALTH_CHECKING = "health_checking"
    RUNNING = "running"
    STOPPED = "stopped"
    FAILED = "failed"
    DELETING = "deleting"


class MlflowModelBuild(Base):
    """One immutable serving-image build for a user-visible model version."""

    __tablename__ = "mlflow_model_builds"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    model_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    source_uri: Mapped[str] = mapped_column(Text, default="", nullable=False)
    model_uri: Mapped[str] = mapped_column(String(1024), nullable=False)
    recipe_version: Mapped[str] = mapped_column(
        String(32), default="mlflow-v1", server_default="mlflow-v1", nullable=False
    )
    workspace_image_id: Mapped[str | None] = mapped_column(
        ForeignKey("workspace_images.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[MlflowModelBuildStatus] = mapped_column(
        Enum(MlflowModelBuildStatus),
        default=MlflowModelBuildStatus.QUEUED,
        nullable=False,
        index=True,
    )
    status_message: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_owner: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MlflowDeployment(Base):
    """A user-owned model service backed by a managed workspace."""

    __tablename__ = "mlflow_deployments"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    build_id: Mapped[str | None] = mapped_column(
        ForeignKey("mlflow_model_builds.id", ondelete="SET NULL"), nullable=True, index=True
    )
    workspace_id: Mapped[str | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="SET NULL"), nullable=True, unique=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    model_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    source_uri: Mapped[str] = mapped_column(Text, default="", nullable=False)
    flavor_id: Mapped[str] = mapped_column(String(50), nullable=False)
    auto_stop_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    gunicorn_workers: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    quota_reserved: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False, index=True
    )
    access_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[MlflowDeploymentStatus] = mapped_column(
        Enum(MlflowDeploymentStatus),
        default=MlflowDeploymentStatus.QUEUED,
        nullable=False,
        index=True,
    )
    status_message: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_owner: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class MlflowDeploymentEvent(Base):
    """Ordered, durable progress entry for a deployment."""

    __tablename__ = "mlflow_deployment_events"
    __table_args__ = (
        UniqueConstraint("deployment_id", "sequence", name="uq_mlflow_deployment_event_sequence"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    deployment_id: Mapped[str] = mapped_column(
        ForeignKey("mlflow_deployments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    level: Mapped[str] = mapped_column(String(16), default="info", nullable=False)
    message: Mapped[str] = mapped_column(String(1000), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
