from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from app.api.routes.impaya import payment_start_url
from app.core.config import get_settings
from app.db.models import User
from app.db.session import SessionLocal

router = Router(name="impaya_payments")
settings = get_settings()


def payment_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Перейти к оплате", url=url)],
        ]
    )


async def _find_user(telegram_id: int) -> User | None:
    async with SessionLocal() as session:
        return await session.scalar(select(User).where(User.telegram_id == telegram_id))


async def _send_payment(message: Message, telegram_id: int) -> None:
    if not settings.impaya_enabled:
        await message.answer("Платёжная система временно недоступна.")
        return
    user = await _find_user(telegram_id)
    if not user:
        await message.answer("Сначала отправьте /start.")
        return
    url = payment_start_url(user, settings)
    mode = "тестовая оплата" if settings.impaya_test_mode else "оплата"
    await message.answer(
        "<b>💳 Подписка Phantom</b>\n\n"
        f"Стоимость первого дня VIP-доступа: <b>{settings.impaya_initial_amount_rub} ₽</b>.\n"
        f"Сейчас используется <b>{mode}</b> через Impaya.\n\n"
        "После подтверждения платежа доступ активируется автоматически.",
        reply_markup=payment_keyboard(url),
    )


@router.message(Command("pay"))
async def pay_command(message: Message) -> None:
    if not message.from_user:
        return
    await _send_payment(message, message.from_user.id)


@router.callback_query(F.data == "impaya:pay")
async def pay_callback(callback: CallbackQuery) -> None:
    if callback.message:
        await _send_payment(callback.message, callback.from_user.id)
    await callback.answer()
