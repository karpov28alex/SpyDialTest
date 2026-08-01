from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime

import structlog
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from redis.asyncio import Redis
from sqlalchemy import or_, select

from app.bot.setup import bot
from app.core.config import get_settings
from app.db.models import User
from app.db.session import SessionLocal
from app.services.access import has_access
from app.services.access_funnel import get_funnel_config
from app.services.users import referral_code

settings = get_settings()
logger = structlog.get_logger()


def expired_keyboard(*, payment_url: str, payment_text: str, referral_available: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if referral_available:
        rows.append([InlineKeyboardButton(text="👥 Пригласить друга", callback_data="funnel:invite")])
    if payment_url.startswith("https://"):
        rows.append([InlineKeyboardButton(text=payment_text, url=payment_url)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def notify_expired_users(redis: Redis) -> int:
    config = await get_funnel_config(redis)
    if not config.enabled:
        return 0

    now = datetime.now(UTC)
    async with SessionLocal() as session:
        users = list((await session.scalars(
            select(User)
            .where(
                User.is_access_disabled.is_(False),
                User.blocked_bot_at.is_(None),
                User.trial_ends_at <= now,
                or_(User.vip_ends_at.is_(None), User.vip_ends_at <= now),
            )
            .order_by(User.id)
            .limit(500)
        )).all())

    sent = 0
    for user in users:
        if has_access(user, now):
            continue
        referral_available = config.referral_required and user.referral_bonus_granted_at is None
        stage = "referral" if referral_available else "payment"
        deadline = int(user.trial_ends_at.timestamp())
        marker = f"phantom:funnel:expiration_notice:{stage}:{user.id}:{deadline}"
        if not await redis.set(marker, "1", ex=60 * 60 * 24 * 180, nx=True):
            continue
        text = config.referral_text if referral_available else config.payment_required_text
        try:
            await bot.send_message(
                user.telegram_id,
                text,
                reply_markup=expired_keyboard(
                    payment_url=config.payment_url,
                    payment_text=config.payment_button_text,
                    referral_available=referral_available,
                ),
            )
            sent += 1
        except Exception:
            await redis.delete(marker)
            logger.exception("funnel_expiration_notification_failed", user_id=user.id)
    return sent


async def funnel_scheduler_loop() -> None:
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        while True:
            try:
                sent = await notify_expired_users(redis)
                if sent:
                    logger.info("funnel_expiration_notifications_sent", count=sent)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("funnel_scheduler_iteration_failed")
            await asyncio.sleep(60)
    finally:
        with suppress(Exception):
            await redis.aclose()
