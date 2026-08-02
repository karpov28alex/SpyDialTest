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
from app.services.access_funnel import get_funnel_config


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
    funnel = await get_funnel_config()
    user = await session.scalar(select(User).where(User.telegram_id == telegram_id).with_for_update())
    created = user is None
    if user is None:
        prerequisites_pending = funnel.enabled and (funnel.channel_required or funnel.business_required)
        trial_end = now if prerequisites_pending else (
            now + timedelta(days=config.trial_days) if config.free_trial_enabled else now
        )
        status = SubscriptionStatus.expired if prerequisites_pending or not config.free_trial_enabled else SubscriptionStatus.trial
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
            subscription_status=status,
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


async def has_active_business(session: AsyncSession, user_id: int) -> bool:
    count = await session.scalar(
        select(func.count(BusinessConnection.id)).where(
            BusinessConnection.owner_user_id == user_id,
            BusinessConnection.is_active.is_(True),
        )
    )
    return bool(count)


async def synchronize_trial_access(
    session: AsyncSession,
    *,
    user: User,
    channel_verified: bool,
    now: datetime | None = None,
) -> bool:
    now = now or datetime.now(UTC)
    funnel = await get_funnel_config()
    monetization = await get_monetization_settings(session)

    if not funnel.enabled:
        return False
    if user.vip_ends_at and user.vip_ends_at > now:
        return False
    if user.referral_bonus_granted_at is not None:
        return False

    business_connected = await has_active_business(session, user.id)
    channel_ready = (not funnel.channel_required) or channel_verified
    business_ready = (not funnel.business_required) or business_connected
    prerequisites_ready = channel_ready and business_ready

    active_free_trial = (
        user.subscription_status == SubscriptionStatus.trial
        and user.trial_ends_at > now
    )
    if not prerequisites_ready:
        if active_free_trial:
            user.trial_started_at = now
            user.trial_ends_at = now
            user.subscription_status = SubscriptionStatus.expired
            await session.flush()
        return False

    is_pending = user.trial_ends_at <= user.trial_started_at
    if not is_pending:
        return False

    user.trial_started_at = now
    user.trial_ends_at = now + timedelta(days=monetization.trial_days) if monetization.free_trial_enabled else now
    user.subscription_status = SubscriptionStatus.trial if monetization.free_trial_enabled else SubscriptionStatus.expired
    await session.flush()
    return monetization.free_trial_enabled


async def activate_trial_after_channel(
    session: AsyncSession,
    *,
    user: User,
    now: datetime | None = None,
) -> bool:
    return await synchronize_trial_access(
        session,
        user=user,
        channel_verified=True,
        now=now,
    )


def activate_business_trial(*args, **kwargs):
    """Compatibility adapter for old and new Business-connection callers.

    Old code called ``activate_business_trial(user, now)`` synchronously. That
    call now safely leaves the trial pending; the next verified bot/Mini App
    access starts it. New code may await the returned coroutine with
    ``session=..., user=..., channel_verified=...``.
    """
    if args and isinstance(args[0], User):
        return False

    async def _activate() -> bool:
        session: AsyncSession = kwargs["session"]
        user: User = kwargs["user"]
        channel_verified: bool = bool(kwargs.get("channel_verified"))
        now: datetime | None = kwargs.get("now")
        return await synchronize_trial_access(
            session,
            user=user,
            channel_verified=channel_verified,
            now=now,
        )

    return _activate()


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
    session.add(Referral(referrer_user_id=referrer.id, referred_user_id=referred.id, code=code, joined_at=now))
    return True


async def qualify_referral(session: AsyncSession, *, referred_user_id: int) -> User | None:
    referral = await session.scalar(
        select(Referral).where(Referral.referred_user_id == referred_user_id).with_for_update()
    )
    if referral is None or referral.bonus_granted_at is not None:
        return None

    referred = await session.get(User, referred_user_id)
    if referred is None:
        return None

    from app.bot.setup import bot
    from app.services.access_funnel import channel_gate_passed

    funnel = await get_funnel_config()
    if not funnel.enabled or not funnel.referral_required:
        return None
    if funnel.channel_required and not await channel_gate_passed(
        bot,
        user_id=referred.telegram_id,
        config=funnel,
    ):
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

    await bot.send_message(
        referrer.telegram_id,
        funnel.referral_bonus_success_text.format(days=config.referral_bonus_days),
    )
    return None


def new_idempotency_key(prefix: str) -> str:
    return f"{prefix}:{secrets.token_urlsafe(18)}"
