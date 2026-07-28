from datetime import datetime

from app.api.routes.admin import _database_now


def test_database_now_is_naive_utc_compatible() -> None:
    value = _database_now()

    assert isinstance(value, datetime)
    assert value.tzinfo is None
