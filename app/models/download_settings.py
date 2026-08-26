from sqlalchemy import Integer, String
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
