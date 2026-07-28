from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.access_models import AppMonetizationSettings
from app.db.models import Subscription, SubscriptionStatus, User


@dataclass(slots=True)
class AccessState:
    active: bool
    source: str
    ends_at: datetime | None
    needs_payment: bool


async def get_monetization_settings(session: AsyncSession, *, lock: bool = False) -> AppMonetizationSettings:
    stmt = select(AppMonetizationSettings).where(AppMonetizationSettings.id == 1)
    if lock:
        stmt = stmt.with_for_update()
    row = await session.scalar(stmt)
    if row is None:
        row = AppMonetizationSettings(id=1)
        session.add(row)
        await session.flush()
    return row


def access_ends_at(user: User) -> datetime:
    candidates = [user.trial_ends_at]
    if user.vip_ends_at:
        candidates.append(user.vip_ends_at)
    return max(candidates)


def has_access(user: User, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    return not user.is_access_disabled and access_ends_at(user) > now


def refresh_subscription_status(user: User, now: datetime | None = None) -> SubscriptionStatus:
    now = now or datetime.now(UTC)
    if user.is_access_disabled:
        user.subscription_status = SubscriptionStatus.disabled
    elif user.vip_ends_at and user.vip_ends_at > now:
        user.subscription_status = SubscriptionStatus.vip
    elif user.trial_ends_at > now:
        user.subscription_status = SubscriptionStatus.referral if user.referral_bonus_granted_at else SubscriptionStatus.trial
    else:
        user.subscription_status = SubscriptionStatus.expired
    return user.subscription_status


async def latest_manual_access(session: AsyncSession, user_id: int) -> Subscription | None:
    now = datetime.now(UTC)
    return await session.scalar(
        select(Subscription)
        .where(Subscription.user_id == user_id, Subscription.status == "active", Subscription.ends_at > now)
        .order_by(desc(Subscription.ends_at))
        .limit(1)
    )


async def access_state(session: AsyncSession, user: User) -> AccessState:
    now = datetime.now(UTC)
    if user.is_access_disabled:
        return AccessState(False, "disabled", None, True)
    manual = await latest_manual_access(session, user.id)
    if manual:
        return AccessState(True, manual.source, manual.ends_at, False)
    if user.vip_ends_at and user.vip_ends_at > now:
        return AccessState(True, "vip", user.vip_ends_at, False)
    if user.trial_ends_at > now:
        source = "referral" if user.subscription_status == SubscriptionStatus.referral else "trial"
        return AccessState(True, source, user.trial_ends_at, False)
    return AccessState(False, "expired", None, True)


async def grant_access(session: AsyncSession, *, user: User, days: int, source: str = "manual_admin") -> Subscription:
    if days < 1 or days > 3650:
        raise ValueError("days must be between 1 and 3650")
    now = datetime.now(UTC)
    current = await latest_manual_access(session, user.id)
    ends_at = max(now, current.ends_at if current else now) + timedelta(days=days)
    row = Subscription(user_id=user.id, status="active", source=source, starts_at=now, ends_at=ends_at)
    session.add(row)
    user.subscription_status = SubscriptionStatus.active
    user.vip_ends_at = ends_at
    await session.flush()
    return row


def payment_plans(config: AppMonetizationSettings) -> list[dict]:
    return [
        {"id": "entry", "title": "Пробный платный доступ", "amount": config.entry_price_rub, "period": "первые сутки", "description": "Разовая тестовая оплата. Через сутки система предложит недельный тариф."},
        {"id": "week", "title": "Неделя доступа", "amount": config.weekly_price_rub, "period": "7 дней", "description": "Основной тариф после первых суток."},
        {"id": "fallback_3d", "title": "Резервный тариф", "amount": config.fallback_three_day_price_rub, "period": "3 дня", "description": "Запасной вариант, если недельная оплата не прошла."},
    ]
