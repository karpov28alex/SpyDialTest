from __future__ import annotations

from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from app.bot.setup import bot
from app.core.config import get_settings
from app.db.models import User
from app.db.session import SessionLocal
from app.services.access_center import build_access_center

router = Router(name="access-center")
settings = get_settings()


def _format_date(value: str | None) -> str:
    if not value:
        return "не указана"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.strftime("%d.%m.%Y · %H:%M UTC")
    except ValueError:
        return value


def _status_icon(complete: bool, required: bool) -> str:
    if not required:
        return "▫️"
    return "✅" if complete else "⏳"


def _keyboard(status: dict) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    channel = status["channel"]
    if channel["required"] and not channel["verified"] and str(channel.get("url") or "").startswith("https://"):
        rows.append([InlineKeyboardButton(text="📢 Подписаться на канал", url=channel["url"])])
        rows.append([InlineKeyboardButton(text="✅ Проверить подписку", callback_data="funnel:check_channel")])
    if status["stage"] == "referral":
        rows.append([InlineKeyboardButton(text="👥 Пригласить друга", callback_data="funnel:invite")])
    payment_url = str(status["payment"].get("url") or "")
    if status["stage"] in {"referral", "payment"} and payment_url.startswith("https://"):
        rows.append([InlineKeyboardButton(text=status["payment"]["button_text"], url=payment_url)])
    rows.append([InlineKeyboardButton(text="🔄 Обновить статус", callback_data="user:access")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _text(status: dict) -> str:
    lines = [
        "<b>🔐 Центр доступа Phantom</b>",
        "",
        f"Прогресс подключения: <b>{status['progress']}%</b>",
        "",
    ]
    for step in status["steps"]:
        icon = _status_icon(step["complete"], step["required"])
        lines.append(f"{icon} {step['title']}")

    lines.extend([
        "",
        f"<b>Текущий этап:</b> {status['stage']}",
        f"<b>Следующий шаг:</b> {status['next_action']}",
    ])

    if status["access"]["active"]:
        lines.extend([
            "",
            f"<b>Источник доступа:</b> {status['access']['source'] or 'активный доступ'}",
            f"<b>Доступ до:</b> {_format_date(status['access']['ends_at'])}",
        ])
    elif status["trial"]["started_at"]:
        lines.extend([
            "",
            f"<b>Trial начат:</b> {_format_date(status['trial']['started_at'])}",
            f"<b>Trial до:</b> {_format_date(status['trial']['ends_at'])}",
        ])

    lines.extend([
        "",
        f"💳 Первый платёж: <b>{status['payment']['entry_price_rub']} ₽</b>",
        f"🔁 Продление: <b>{status['payment']['weekly_price_rub']} ₽ / 7 дней</b>",
        f"🛟 Резервный тариф: <b>{status['payment']['fallback_price_rub']} ₽ / 3 дня</b>",
    ])
    return "\n".join(lines)


async def _load_status(telegram_id: int) -> dict | None:
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        if not user:
            return None
        return await build_access_center(session=session, user=user, bot=bot)


async def _send(message: Message, telegram_id: int) -> None:
    status = await _load_status(telegram_id)
    if status is None:
        await message.answer("Сначала отправьте /start.")
        return
    await message.answer(_text(status), reply_markup=_keyboard(status))


@router.message(Command("access"))
async def access_command(message: Message) -> None:
    if message.from_user:
        await _send(message, message.from_user.id)


@router.callback_query(F.data == "user:access")
async def access_callback(callback: CallbackQuery) -> None:
    status = await _load_status(callback.from_user.id)
    if status is None:
        await callback.answer("Сначала отправьте /start", show_alert=True)
        return
    if callback.message:
        try:
            await callback.message.edit_text(_text(status), reply_markup=_keyboard(status))
        except Exception:
            await callback.message.answer(_text(status), reply_markup=_keyboard(status))
    await callback.answer("Статус обновлён")
