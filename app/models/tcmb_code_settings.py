from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class TcmbCodeSettings(Base):
    """Singleton Admin settings for the TCMB Code VS Code workspace.

    The chat gateway, model catalog and default model come from Workspace AI
    (``jupyter_ai_settings``); this table only holds what TCMB Code adds.
    """

    __tablename__ = "tcmb_code_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    library_url: Mapped[str] = mapped_column(String(512), default="", nullable=False)
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
