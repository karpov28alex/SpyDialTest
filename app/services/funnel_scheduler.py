from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import structlog
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from redis.asyncio import Redis
from sqlalchemy import or_, select

from app.bot.setup import bot
from app.core.config import get_settings
from app.db.models import Payment, Subscription, SubscriptionStatus, User
from app.db.session import SessionLocal
from app.services.access import get_monetization_settings, has_access
from app.services.access_funnel import get_funnel_config
from app.services.impaya import ImpayaClient, ImpayaError, successful_state

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
        users = list((await session.scalars(select(User).where(User.is_access_disabled.is_(False), User.blocked_bot_at.is_(None), User.trial_ends_at <= now, or_(User.vip_ends_at.is_(None), User.vip_ends_at <= now)).order_by(User.id).limit(500))).all())
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
            await bot.send_message(user.telegram_id, text, reply_markup=expired_keyboard(payment_url=config.payment_url, payment_text=config.payment_button_text, referral_available=referral_available))
            sent += 1
        except Exception:
            await redis.delete(marker)
            logger.exception("funnel_expiration_notification_failed", user_id=user.id)
    return sent


async def _billing_source(session, user_id: int) -> Payment | None:
    payments = list((await session.scalars(select(Payment).where(Payment.user_id == user_id, Payment.provider == "impaya", Payment.recurring.is_(True)).order_by(Payment.id.desc()))).all())
    for payment in payments:
        payload = payment.payload if isinstance(payment.payload, dict) else {}
        if payload.get("binding_id") and payload.get("impaya_user_id"):
            return payment
    return None


async def _renewal_attempt(session, *, user: User, source: Payment, operation_id: str, amount_rub: int, access_days: int, kind: str) -> Payment:
    existing = await session.scalar(select(Payment).where(Payment.external_id == operation_id))
    if existing:
        return existing
    source_payload = source.payload if isinstance(source.payload, dict) else {}
    payment = Payment(user_id=user.id, provider="impaya", external_id=operation_id, amount=Decimal(amount_rub), currency="RUB", status="pending", recurring=True, payload={"kind": kind, "source_payment_id": source.id, "binding_id": source_payload.get("binding_id"), "impaya_user_id": source_payload.get("impaya_user_id"), "merchant_user_id": source_payload.get("merchant_user_id", ""), "access_days": access_days})
    session.add(payment)
    await session.flush()
    client = ImpayaClient(settings)
    try:
        response = await client.recurrent_pay(customer_operation_id=operation_id, amount_rub=amount_rub, binding_id=str(source_payload["binding_id"]), impaya_user_id=str(source_payload["impaya_user_id"]), merchant_user_id=str(source_payload.get("merchant_user_id") or ""), description=f"Продление VIP-доступа Phantom на {access_days} дней")
        state = await client.transaction_state(customer_operation_id=operation_id, extended=True, terminal_name=settings.impaya_non3ds_terminal_name)
        payment.payload = {**payment.payload, "response": response, "state": state}
    except ImpayaError as exc:
        payment.status = "failed"
        payment.payload = {**payment.payload, "error_code": exc.code, "error_message": str(exc), "error_response": exc.payload}
        await session.commit()
        return payment
    if not successful_state(state):
        payment.status = "failed"
        await session.commit()
        return payment
    now = datetime.now(UTC)
    new_end = now + timedelta(days=access_days)
    user.vip_ends_at = new_end
    user.subscription_status = SubscriptionStatus.vip
    payment.status = "paid"
    payment.paid_at = now
    payment.payload = {**payment.payload, "new_vip_ends_at": new_end.isoformat()}
    session.add(Subscription(user_id=user.id, status="active", source=f"impaya_{kind}", starts_at=now, ends_at=new_end))
    await session.commit()
    return payment


async def process_impaya_renewals() -> int:
    if not settings.impaya_enabled or not settings.impaya_renewal_enabled:
        return 0
    now = datetime.now(UTC)
    async with SessionLocal() as session:
        monetization = await get_monetization_settings(session)
        renewal_amount = int(monetization.weekly_price_rub or settings.impaya_renewal_amount_rub)
        fallback_amount = int(monetization.fallback_three_day_price_rub or settings.impaya_fallback_amount_rub)
        users = list((await session.scalars(select(User).where(User.is_access_disabled.is_(False), User.vip_ends_at.is_not(None), User.vip_ends_at <= now).order_by(User.id).limit(100))).all())
        renewed = 0
        for user in users:
            source = await _billing_source(session, user.id)
            if not source:
                continue
            source_payload = source.payload if isinstance(source.payload, dict) else {}
            if source_payload.get("auto_renew", True) is False:
                continue
            period_key = int(user.vip_ends_at.timestamp()) if user.vip_ends_at else int(now.timestamp())
            primary = await _renewal_attempt(session, user=user, source=source, operation_id=f"ph_renew_{user.id}_{period_key}", amount_rub=renewal_amount, access_days=settings.impaya_renewal_access_days, kind="renewal")
            result = primary
            if primary.status != "paid" and settings.impaya_fallback_enabled:
                result = await _renewal_attempt(session, user=user, source=source, operation_id=f"ph_fallback_{user.id}_{period_key}", amount_rub=fallback_amount, access_days=settings.impaya_fallback_access_days, kind="fallback")
            try:
                if result.status == "paid":
                    renewed += 1
                    days = int((result.payload or {}).get("access_days") or 0)
                    await bot.send_message(user.telegram_id, "<b>✅ Подписка продлена</b>\n\n" f"Списано: <b>{result.amount} ₽</b>\n" f"VIP-доступ продлён на <b>{days} дней</b>.")
                else:
                    await bot.send_message(user.telegram_id, "<b>⚠️ Не удалось продлить подписку</b>\n\nАвтоматическое списание отклонено. Откройте /pay, чтобы оплатить другой картой.")
            except Exception:
                logger.exception("impaya_renewal_notification_failed", user_id=user.id)
        return renewed


async def funnel_scheduler_loop() -> None:
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        while True:
            try:
                renewed = await process_impaya_renewals()
                sent = await notify_expired_users(redis)
                if renewed:
                    logger.info("impaya_subscriptions_renewed", count=renewed)
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
