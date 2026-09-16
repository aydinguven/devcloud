from sqlalchemy import Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class SessionSettings(Base):
    """Persisted singleton policy for newly issued login sessions."""

    __tablename__ = "session_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    timeout_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=240)
