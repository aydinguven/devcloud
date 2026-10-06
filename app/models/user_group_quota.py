from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# The row with this key holds the default per-user quota for users without a
# directory team (and for teams without their own quota values).
DEFAULT_GROUP_KEY = ""


class UserGroupQuota(Base):
    """Per-member quota for one directory team (AD ``department``).

    Every member receives these limits individually; they are not a shared
    team budget. A NULL field inherits the default group's value.
    """

    __tablename__ = "user_group_quotas"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    group_key: Mapped[str] = mapped_column(
        String(255), unique=True, index=True, nullable=False
    )
    display_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    cpu_quota: Mapped[float | None] = mapped_column(Float, nullable=True)
    memory_mb_quota: Mapped[int | None] = mapped_column(Integer, nullable=True)
    disk_mb_quota: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gpu_quota: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
