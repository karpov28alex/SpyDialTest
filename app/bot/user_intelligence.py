from __future__ import annotations

import csv
import io
import json

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo
from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import Dialog, Message as DbMessage, User
from app.db.session import SessionLocal
from app.services.access import access_state
from app.services.intelligence import build_user_intelligence

router = Router(name="user-intelligence")
settings = get_settings()


def menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Общая статистика", callback_data="intel:summary")],
        [InlineKeyboardButton(text="🏆 ТОП собеседников", callback_data="intel:leaders")],
        [InlineKeyboardButton(text="🕓 Активность", callback_data="intel:activity"), InlineKeyboardButton(text="📸 Медиа", callback_data="intel:media")],
        [InlineKeyboardButton(text="📥 Экспорт CSV", callback_data="intel:export:csv"), InlineKeyboardButton(text="📦 Экспорт JSON", callback_data="intel:export:json")],
        [InlineKeyboardButton(text="📱 Открыть статистику", web_app=WebAppInfo(url=f"{settings.mini_app_url}?screen=stats"))],
    ])


async def _user(telegram_id: int) -> User | None:
    async with SessionLocal() as session:
        return await session.scalar(select(User).where(User.telegram_id == telegram_id))


async def _data(telegram_id: int, days: int = 30):
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        if not user:
            return None, None
        return user, await build_user_intelligence(session, user, days=days)


def _summary(data: dict) -> str:
    t = data["totals"]
    locked = data.get("locked")
    text = (
        "<b>📊 Phantom Intelligence</b>\n\n"
        f"💬 Диалогов: <b>{t['dialogs']}</b>\n"
        f"✉️ Сообщений: <b>{t['messages']}</b>\n"
        f"📸 Медиа: <b>{t['media']}</b>\n"
        f"🗑 Удалённых: <b>{t['deleted']}</b>\n"
        f"✏️ Изменённых: <b>{t['edited']}</b>\n"
        f"👻 Скрытых медиа: <b>{t['protected']}</b>"
    )
    if locked:
        text += "\n\n🔒 Подробная аналитика и экспорт доступны после оплаты."
    return text


@router.message(Command("stats"))
async def stats_command(message: Message) -> None:
    if not message.from_user:
        return
    _, data = await _data(message.from_user.id)
    if not data:
        await message.answer("Сначала запустите бота командой /start.")
        return
    await message.answer(_summary(data), reply_markup=menu())


@router.callback_query(F.data.startswith("intel:"))
async def callbacks(callback: CallbackQuery) -> None:
    if not callback.message:
        await callback.answer()
        return
    parts = (callback.data or "").split(":")
    action = parts[1]
    user, data = await _data(callback.from_user.id)
    if not user or not data:
        await callback.answer("Пользователь не найден", show_alert=True)
        return

    if action == "summary":
        await callback.message.answer(_summary(data), reply_markup=menu())
    elif action == "leaders":
        leaders = data["leaders"]
        def row(title: str, item):
            if not item:
                return f"{title}: недостаточно данных"
            username = f" (@{item['username']})" if item.get("username") else ""
            return f"{title}: <b>{item['name']}</b>{username} — {item['value']}"
        await callback.message.answer(
            "<b>🏆 Лидеры общения</b>\n\n"
            + "\n".join([
                row("Больше всего сообщений", leaders.get("active")),
                row("Больше всего медиа", leaders.get("media")),
                row("Больше всего удалений", leaders.get("deleted")),
                row("Больше всего скрытых медиа", leaders.get("protected")),
            ]),
            reply_markup=menu(),
        )
    elif action == "activity":
        hours = sorted(data["hours"], key=lambda x: x["messages"], reverse=True)[:5]
        lines = [f"{x['hour']:02d}:00 — <b>{x['messages']}</b> сообщений" for x in hours]
        await callback.message.answer(
            "<b>🕓 Активность за 30 дней</b>\n\n" + ("\n".join(lines) if lines else "Недостаточно данных"),
            reply_markup=menu(),
        )
    elif action == "media":
        t = data["totals"]
        await callback.message.answer(
            "<b>📸 Медиа</b>\n\n"
            f"Всего вложений: <b>{t['media']}</b>\n"
            f"Скрытых медиа: <b>{t['protected']}</b>",
            reply_markup=menu(),
        )
    elif action == "export":
        if data.get("locked"):
            await callback.answer("Экспорт доступен после оплаты", show_alert=True)
            return
        format_name = parts[2] if len(parts) > 2 else "csv"
        async with SessionLocal() as session:
            rows = (
                await session.execute(
                    select(Dialog, DbMessage)
                    .join(DbMessage, DbMessage.dialog_id == Dialog.id)
                    .where(Dialog.owner_user_id == user.id)
                    .order_by(DbMessage.sent_at, DbMessage.id)
                )
            ).all()
        if format_name == "json":
            payload = json.dumps([
                {
                    "dialog": dialog.peer_name or dialog.peer_username,
                    "username": dialog.peer_username,
                    "direction": message.direction,
                    "text": message.text or message.caption,
                    "sent_at": message.sent_at.isoformat() if message.sent_at else None,
                    "edited_at": message.edited_at.isoformat() if message.edited_at else None,
                    "deleted_at": message.deleted_at.isoformat() if message.deleted_at else None,
                }
                for dialog, message in rows
            ], ensure_ascii=False, indent=2).encode("utf-8")
            await callback.message.answer_document(BufferedInputFile(payload, filename="phantom-archive.json"))
        else:
            stream = io.StringIO()
            writer = csv.writer(stream, delimiter=";")
            writer.writerow(["dialog", "username", "direction", "sent_at", "edited_at", "deleted_at", "text"])
            for dialog, message in rows:
                writer.writerow([
                    dialog.peer_name or dialog.peer_username or "",
                    dialog.peer_username or "",
                    message.direction,
                    message.sent_at.isoformat() if message.sent_at else "",
                    message.edited_at.isoformat() if message.edited_at else "",
                    message.deleted_at.isoformat() if message.deleted_at else "",
                    message.text or message.caption or "",
                ])
            await callback.message.answer_document(
                BufferedInputFile(("\ufeff" + stream.getvalue()).encode("utf-8"), filename="phantom-archive.csv")
            )
    await callback.answer()
