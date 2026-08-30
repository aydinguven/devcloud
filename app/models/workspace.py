import enum
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from sqlalchemy import String, Integer, DateTime, Enum, ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.node import Node
    from app.models.user import User


class WorkspaceStatus(str, enum.Enum):
    CREATING = "creating"
    RUNNING = "running"
    STOPPED = "stopped"
    STARTING = "starting"
    STOPPING = "stopping"
    ERROR = "error"
    DELETED = "deleted"


class Workspace(Base):
    """Workspace instance deployed as a container."""
    __tablename__ = "workspaces"
    __table_args__ = (
        UniqueConstraint("node_id", "host_port", name="uq_workspaces_node_host_port"),
        UniqueConstraint(
            "node_id",
            "accelerator_device_id",
            "accelerator_slot",
            name="uq_workspaces_accelerator_slot",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    
    # Ownership
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    owner: Mapped["User"] = relationship("User", back_populates="workspaces")

    # Every workspace belongs to a worker. The controller has no local
    # container runtime, including in an all-in-one deployment.
    node_id: Mapped[str] = mapped_column(
        ForeignKey("nodes.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    node: Mapped["Node"] = relationship("Node", back_populates="workspaces")

    # Specifications
    template_id: Mapped[str] = mapped_column(String(50), nullable=False)  # vscode-empty, vscode-python, etc.
    flavor_id: Mapped[str] = mapped_column(String(50), nullable=False)    # t1.nano through t1.xlarge
    accelerator_device_id: Mapped[str | None] = mapped_column(
        String(160), nullable=True
    )
    accelerator_cdi_name: Mapped[str | None] = mapped_column(
        String(200), nullable=True
    )
    accelerator_model: Mapped[str | None] = mapped_column(
        String(160), nullable=True
    )
    accelerator_kind: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    accelerator_slot: Mapped[int | None] = mapped_column(Integer, nullable=True)
    accelerator_memory_mb: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    accelerator_shared_slots: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )

    # Container details
    container_id: Mapped[str] = mapped_column(String(128), nullable=True)
    container_name: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    host_port: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    container_port: Mapped[int] = mapped_column(Integer, default=8080, nullable=False)
    workspace_token: Mapped[str] = mapped_column(String(128), default=lambda: uuid.uuid4().hex, nullable=False)
    
    # Persistence
    storage_path: Mapped[str] = mapped_column(String(512), nullable=False)
    
    # Lifecycle
    status: Mapped[WorkspaceStatus] = mapped_column(
        Enum(WorkspaceStatus), default=WorkspaceStatus.CREATING, nullable=False
    )
    error_message: Mapped[str] = mapped_column(Text, nullable=True)
    auto_stop_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # 0 = disabled
    
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False
    )
    last_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    last_stopped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<Workspace id={self.id} name='{self.name}' status='{self.status}'>"
