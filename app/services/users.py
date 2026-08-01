import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    BusinessConnection,
    Message,
    Referral,
    SubscriptionStatus,
    User,
    UserSettings,
)
from app.services.access import get_monetization_settings


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
    config = await get_monetization_settings(session)
    user = await session.scalar(select(User).where(User.telegram_id == telegram_id).with_for_update())
    created = user is None
    if user is None:
        trial_end = now + timedelta(days=config.trial_days) if config.free_trial_enabled else now
        user = User(
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
            language_code=language_code,
            registered_at=now,
            last_seen_at=now,
            trial_started_at=now,
            trial_ends_at=trial_end,
            subscription_status=SubscriptionStatus.trial if config.free_trial_enabled else SubscriptionStatus.expired,
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


def activate_business_trial(user: User, now: datetime | None = None) -> bool:
    """Backward compatible hook. Trial is now assigned at registration when enabled."""
    return False


def referral_code(user: User) -> str:
    return f"{user.id:x}{user.telegram_id:x}"[-24:]


async def apply_referral(session: AsyncSession, *, referred: User, code: str) -> bool:
    """Attach a referral without granting the bonus prematurely.

    The referrer receives the bonus only after the referred user connects
    Telegram Business and at least one business message is archived.
    """
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
    session.add(Referral(referrer_user_id=referrer.id, referred_user_id=referred.id, code=code, joined_at=now))
    return True


async def qualify_referral(session: AsyncSession, *, referred_user_id: int) -> User | None:
    """Grant one referral bonus after genuine Telegram Business usage."""
    referral = await session.scalar(
        select(Referral)
        .where(Referral.referred_user_id == referred_user_id)
        .with_for_update()
    )
    if referral is None or referral.bonus_granted_at is not None:
        return None

    active_business = await session.scalar(
        select(func.count(BusinessConnection.id)).where(
            BusinessConnection.owner_user_id == referred_user_id,
            BusinessConnection.is_active.is_(True),
        )
    )
    if not active_business:
        return None

    archived_messages = await session.scalar(
        select(func.count(Message.id))
        .join(BusinessConnection, BusinessConnection.id == Message.business_connection_id)
        .where(BusinessConnection.owner_user_id == referred_user_id)
    )
    if not archived_messages:
        return None

    referrer = await session.get(User, referral.referrer_user_id, with_for_update=True)
    if referrer is None or referrer.referral_bonus_granted_at is not None:
        return None

    config = await get_monetization_settings(session)
    now = datetime.now(UTC)
    referrer.trial_ends_at = max(now, referrer.trial_ends_at) + timedelta(days=config.referral_bonus_days)
    referrer.referral_bonus_granted_at = now
    referrer.subscription_status = SubscriptionStatus.referral
    referral.bonus_granted_at = now
    await session.flush()
    return referrer


def new_idempotency_key(prefix: str) -> str:
    return f"{prefix}:{secrets.token_urlsafe(18)}"
