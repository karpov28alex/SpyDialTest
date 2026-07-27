from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.services.access import access_ends_at, has_access


def test_access_uses_latest_entitlement() -> None:
    now = datetime.now(UTC)
    user = SimpleNamespace(
        trial_ends_at=now + timedelta(hours=1),
        vip_ends_at=now + timedelta(days=3),
        is_access_disabled=False,
    )
    assert access_ends_at(user) == user.vip_ends_at
    assert has_access(user, now)


def test_disabled_user_has_no_access() -> None:
    now = datetime.now(UTC)
    user = SimpleNamespace(
        trial_ends_at=now + timedelta(days=3),
        vip_ends_at=None,
        is_access_disabled=True,
    )
    assert not has_access(user, now)
