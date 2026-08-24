import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Integer, DateTime, Text, Boolean
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


class CustomTemplate(Base):
    """Admin-defined or user-snapshotted custom workspace template."""
    __tablename__ = "custom_templates"

    id: Mapped[str] = mapped_column(String(50), primary_key=True)  # e.g. custom-pytorch, custom-rust
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    category: Mapped[str] = mapped_column(String(50), default="Custom", nullable=False)
    icon: Mapped[str] = mapped_column(String(50), default="📦", nullable=False)
    image_tag: Mapped[str] = mapped_column(String(255), nullable=False)
    default_port: Mapped[int] = mapped_column(Integer, default=8080, nullable=False)
    ide_type: Mapped[str] = mapped_column(String(50), default="vscode", nullable=False)  # vscode or jupyter
    containerfile: Mapped[str] = mapped_column(Text, default="", nullable=False)
    is_ready: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
