from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class KiloCodeSettings(Base):
    """Singleton Admin settings for the Kilo Code VS Code workspaces.

    The chat gateway, model catalog and default model come from Workspace AI
    (``jupyter_ai_settings``); this table only holds what Kilo Code adds.
    """

    __tablename__ = "kilo_code_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Catalog root (no /mcp suffix): the remote MCP server and LocalMCP.sh use it.
    catalog_url: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    encrypted_catalog_token: Mapped[str] = mapped_column(
        Text, default="", nullable=False
    )
    # JSON list of URLs Kilo Code discovers skills from.
    skills_urls_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    # JSON list of Workspace AI catalog model IDs; empty offers the whole catalog.
    model_ids_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    indexing_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    indexing_model: Mapped[str] = mapped_column(
        String(255), default="bge-m3", nullable=False
    )
    indexing_dimension: Mapped[int] = mapped_column(
        Integer, default=1024, nullable=False
    )
    encrypted_indexing_key: Mapped[str] = mapped_column(
        Text, default="", nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
