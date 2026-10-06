from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class GenAiAccount(Base):
    """A devcloud user's LiteLLM identity and the keys devcloud issued for it.

    The personal API key is shown to the user once and never stored; only the
    LiteLLM token hash is kept so the key can be deleted on rotation.
    """

    __tablename__ = "genai_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
    )
    litellm_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    personal_key_alias: Mapped[str] = mapped_column(
        String(255), default="", nullable=False
    )
    personal_key_token: Mapped[str] = mapped_column(
        String(255), default="", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    rotated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
