from datetime import datetime, timedelta, timezone

from app.time_utils import ensure_utc


def test_ensure_utc_treats_naive_database_value_as_utc():
    value = datetime(2026, 8, 25, 9, 30)

    normalized = ensure_utc(value)

    assert normalized == datetime(2026, 8, 25, 9, 30, tzinfo=timezone.utc)


def test_ensure_utc_converts_aware_value_to_utc():
    value = datetime(2026, 8, 25, 12, 30, tzinfo=timezone(timedelta(hours=3)))

    normalized = ensure_utc(value)

    assert normalized == datetime(2026, 8, 25, 9, 30, tzinfo=timezone.utc)
