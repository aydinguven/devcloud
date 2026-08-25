"""Datetime helpers for values persisted by timezone-limited databases."""

from datetime import datetime, timezone


def ensure_utc(value: datetime) -> datetime:
    """Return an aware UTC datetime, treating persisted naive values as UTC."""
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
