from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.config import settings
from app.database import Base


class DownloadSettings(Base):
    """Singleton settings for public offline downloads and worker bootstrap."""

    __tablename__ = "download_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    public_base_url: Mapped[str] = mapped_column(
        String(1024),
        default=lambda: settings.DOWNLOAD_PUBLIC_BASE_URL,
        nullable=False,
    )
    https_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    https_hostname: Mapped[str] = mapped_column(
        String(253),
        default=lambda: settings.HTTPS_DEFAULT_HOSTNAME,
        nullable=False,
    )
    http_fallback_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    certificate_subject: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    certificate_not_after: Mapped[str | None] = mapped_column(String(64), nullable=True)
    certificate_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
