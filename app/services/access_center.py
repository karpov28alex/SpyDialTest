from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BusinessConnection, User
from app.services.access import access_state, get_monetization_settings
from app.services.access_funnel import channel_gate_passed, get_funnel_config


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


async def build_access_center(*, session: AsyncSession, user: User, bot) -> dict[str, Any]:
    funnel = await get_funnel_config()
    monetization = await get_monetization_settings(session)
    access = await access_state(session, user)

    business_connected = bool(
        await session.scalar(
            select(BusinessConnection.id).where(
                BusinessConnection.owner_user_id == user.id,
                BusinessConnection.is_active.is_(True),
            ).limit(1)
        )
    )
    channel_verified = (
        True
        if not funnel.enabled or not funnel.channel_required
        else await channel_gate_passed(bot, user_id=user.telegram_id, config=funnel)
    )

    referral_available = bool(funnel.referral_required and user.referral_bonus_granted_at is None)
    trial_started = user.trial_started_at is not None
    trial_ends_at = user.trial_ends_at

    if funnel.enabled and funnel.channel_required and not channel_verified:
        stage = "channel"
        next_action = "Подпишитесь на информационный канал и подтвердите подписку."
    elif funnel.enabled and funnel.business_required and not business_connected and not access.active:
        stage = "business"
        next_action = "Подключите Phantom к Telegram Business — после этого начнётся пробный период."
    elif access.active:
        stage = "active"
        if access.source == "trial":
            next_action = "Пробный период активен. Подготовьте приглашение друга или оплату до его окончания."
        else:
            next_action = "Доступ активен. Следите за датой окончания и статусом автопродления."
    elif referral_available:
        stage = "referral"
        next_action = "Пригласите друга, который подключит Telegram Business, либо оплатите доступ."
    else:
        stage = "payment"
        next_action = "Для продолжения работы необходимо оплатить доступ."

    steps = [
        {
            "key": "channel",
            "title": "Подписка на канал",
            "required": bool(funnel.enabled and funnel.channel_required),
            "complete": channel_verified,
        },
        {
            "key": "business",
            "title": "Telegram Business",
            "required": bool(funnel.enabled and funnel.business_required),
            "complete": business_connected,
        },
        {
            "key": "trial",
            "title": f"Пробный период · {monetization.trial_days} дн.",
            "required": bool(monetization.free_trial_enabled),
            "complete": trial_started,
        },
        {
            "key": "referral",
            "title": f"Бонус за друга · {monetization.referral_bonus_days} дн.",
            "required": bool(funnel.enabled and funnel.referral_required),
            "complete": user.referral_bonus_granted_at is not None,
        },
        {
            "key": "payment",
            "title": "Оплаченный доступ",
            "required": True,
            "complete": bool(access.active and access.source not in {"trial", "referral"}),
        },
    ]

    required_steps = [item for item in steps if item["required"]]
    completed_steps = [item for item in required_steps if item["complete"]]
    progress = round((len(completed_steps) / len(required_steps)) * 100) if required_steps else 100

    return {
        "stage": stage,
        "progress": progress,
        "next_action": next_action,
        "channel": {
            "required": bool(funnel.enabled and funnel.channel_required),
            "verified": channel_verified,
            "title": funnel.channel_title,
            "url": funnel.channel_url,
        },
        "business": {
            "required": bool(funnel.enabled and funnel.business_required),
            "connected": business_connected,
        },
        "access": {
            "active": access.active,
            "source": access.source,
            "ends_at": _iso(access.ends_at),
            "needs_payment": access.needs_payment,
        },
        "trial": {
            "enabled": monetization.free_trial_enabled,
            "days": monetization.trial_days,
            "started_at": _iso(user.trial_started_at),
            "ends_at": _iso(trial_ends_at),
        },
        "referral": {
            "required": bool(funnel.enabled and funnel.referral_required),
            "available": referral_available,
            "bonus_days": monetization.referral_bonus_days,
            "granted_at": _iso(user.referral_bonus_granted_at),
        },
        "payment": {
            "button_text": funnel.payment_button_text,
            "url": funnel.payment_url,
            "entry_price_rub": monetization.entry_price_rub,
            "weekly_price_rub": monetization.weekly_price_rub,
            "fallback_price_rub": monetization.fallback_three_day_price_rub,
        },
        "steps": steps,
    }
