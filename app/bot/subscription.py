from __future__ import annotations

from datetime import UTC, datetime

from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select

from app.db.models import Payment, User
from app.db.session import SessionLocal


async def subscription_command(message: Message) -> None:
    if not message.from_user:
        return
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.telegram_id == message.from_user.id))
        if not user:
            await message.answer("Сначала отправьте /start.")
            return
        payments = list((await session.scalars(
            select(Payment)
            .where(
                Payment.user_id == user.id,
                Payment.provider == "impaya",
                Payment.recurring.is_(True),
            )
            .order_by(Payment.id.desc())
        )).all())

    source = None
    for payment in payments:
        payload = payment.payload if isinstance(payment.payload, dict) else {}
        if payload.get("binding_id") and payload.get("impaya_user_id"):
            source = payment
            break

    now = datetime.now(UTC)
    active = bool(user.vip_ends_at and user.vip_ends_at > now)
    if not source and not active:
        await message.answer("Актуальной подписки не найдено.")
        return

    payload = source.payload if source and isinstance(source.payload, dict) else {}
    card = payload.get("card") if isinstance(payload.get("card"), dict) else {}
    auto_renew = bool(source and payload.get("auto_renew") is not False)
    card_line = card.get("pan_mask") or "не привязана"
    bank = card.get("bank_name")
    if bank and card_line != "не привязана":
        card_line = f"{card_line} · {bank}"
    until = user.vip_ends_at.strftime("%d.%m.%Y · %H:%M UTC") if user.vip_ends_at else "не определена"
    next_charge = until if active and auto_renew else "не запланировано"

    await message.answer(
        "<b>💳 Подписка Phantom</b>\n\n"
        f"Статус: <b>{'активна' if active else 'неактивна'}</b>\n"
        f"VIP до: <b>{until}</b>\n"
        f"Автопродление: <b>{'включено' if auto_renew else 'отключено'}</b>\n"
        f"Следующее списание: <b>{next_charge}</b>\n"
        f"Карта: <b>{card_line}</b>\n\n"
        "Отключить будущие списания: /cancel"
    )
