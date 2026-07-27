import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Referral, User, UserSettings

TRIAL_DURATION = timedelta(hours=72)
REFERRAL_DURATION = timedelta(hours=72)


async def register_or_update_user(
    session: AsyncSession,
    *,
    telegram_id: int,
    username: str | None,
    first_name: str | None,
    last_name: str | None,
    language_code: str | None,
    start_parameter: str | None = None,
) -> tuple[User, bool]:
    now = datetime.now(UTC)
    user = await session.scalar(select(User).where(User.telegram_id == telegram_id).with_for_update())
    created = user is None
    if user is None:
        user = User(
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
            language_code=language_code,
            registered_at=now,
            last_seen_at=now,
            trial_started_at=now,
            trial_ends_at=now + TRIAL_DURATION,
        )
        user.settings = UserSettings(language=language_code or "ru")
        session.add(user)
        await session.flush()
        if start_parameter and start_parameter.startswith("ref_"):
            await apply_referral(session, referred=user, code=start_parameter.removeprefix("ref_"))
    else:
        user.username = username
        user.first_name = first_name
        user.last_name = last_name
        user.language_code = language_code
        user.last_seen_at = now
    return user, created


def referral_code(user: User) -> str:
    return f"{user.id:x}{user.telegram_id:x}"[-24:]


async def apply_referral(session: AsyncSession, *, referred: User, code: str) -> bool:
    if referred.referrer_user_id is not None:
        return False
    referrer = None
    users = (await session.scalars(select(User))).all()
    for candidate in users:
        if referral_code(candidate) == code:
            referrer = candidate
            break
    if referrer is None or referrer.id == referred.id or referrer.telegram_id == referred.telegram_id:
        return False
    existing = await session.scalar(select(Referral).where(Referral.referred_user_id == referred.id))
    if existing:
        return False
    now = datetime.now(UTC)
    referred.referrer_user_id = referrer.id
    session.add(
        Referral(
            referrer_user_id=referrer.id,
            referred_user_id=referred.id,
            code=code,
            joined_at=now,
        )
    )
    if referrer.referral_bonus_granted_at is None:
        referrer.trial_ends_at = max(now, referrer.trial_ends_at) + REFERRAL_DURATION
        referrer.referral_bonus_granted_at = now
    return True


def new_idempotency_key(prefix: str) -> str:
    return f"{prefix}:{secrets.token_urlsafe(18)}"
