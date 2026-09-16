import uuid
from sqlalchemy import Integer, String, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


class WorkspaceShare(Base):
    __tablename__ = "workspace_shares"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    port: Mapped[int] = mapped_column(Integer)
    password_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    expires_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    revoked: Mapped[bool] = mapped_column(default=False)
    failed_attempts: Mapped[int] = mapped_column(default=0)
    locked_until: Mapped[int] = mapped_column(default=0)
