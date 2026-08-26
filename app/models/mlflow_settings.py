from sqlalchemy import Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class MlflowSettings(Base):
    """Singleton configuration for the server-side MLflow connector."""

    __tablename__ = "mlflow_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    base_url: Mapped[str] = mapped_column(String(1024), default="", nullable=False)
    auth_type: Mapped[str] = mapped_column(String(32), default="none", nullable=False)
    username: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    encrypted_secret: Mapped[str] = mapped_column(Text, default="", nullable=False)
    validate_tls: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    ca_cert_file: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=10, nullable=False)

