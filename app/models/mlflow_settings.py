from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class MlflowSettings(Base):
    """One encrypted server-side MLflow connector configuration per user."""

    __tablename__ = "mlflow_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=True,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    base_url: Mapped[str] = mapped_column(String(1024), default="", nullable=False)
    auth_type: Mapped[str] = mapped_column(String(32), default="none", nullable=False)
    username: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    encrypted_secret: Mapped[str] = mapped_column(Text, default="", nullable=False)
    validate_tls: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    ca_cert_file: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=10, nullable=False)

