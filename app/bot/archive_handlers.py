from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from app.db.models import Dialog, Message as DbMessage, User
from app.db.session import SessionLocal

router = Router(name="telegram-archive")


def archive_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💬 Диалоги", callback_data="user:dialogs"),
            InlineKeyboardButton(text="🕘 Последние события", callback_data="user:recent"),
        ],
        [InlineKeyboardButton(text="↩️ В меню", callback_data="user:menu")],
    ])


async def _user(telegram_id: int) -> User | None:
    async with SessionLocal() as session:
        return await session.scalar(select(User).where(User.telegram_id == telegram_id))


async def dialogs_text(telegram_id: int) -> str:
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            return "Профиль ещё не создан. Отправьте /start."
        rows = list((await session.scalars(
            select(Dialog)
            .where(Dialog.owner_user_id == user.id)
            .order_by(Dialog.last_message_at.desc().nullslast(), Dialog.id.desc())
            .limit(15)
        )).all())
        if not rows:
            return "<b>💬 Диалоги</b>\n\nАрхив пока пуст."
        lines = ["<b>💬 Последние диалоги</b>", ""]
        for row in rows:
            name = html.escape(str(row.peer_name or row.peer_username or row.telegram_chat_id))
            stamp = row.last_message_at.strftime("%d.%m · %H:%M") if row.last_message_at else "—"
            lines.append(f"• <b>{name}</b> — {stamp}")
        lines.extend(["", "Полная переписка и вложения доступны в Mini App."])
        return "\n".join(lines)


async def recent_text(telegram_id: int) -> str:
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            return "Профиль ещё не создан. Отправьте /start."
        rows = list((await session.execute(
            select(DbMessage, Dialog)
            .join(Dialog, Dialog.id == DbMessage.dialog_id)
            .where(Dialog.owner_user_id == user.id)
            .order_by(DbMessage.updated_at.desc(), DbMessage.id.desc())
            .limit(12)
        )).all())
        if not rows:
            return "<b>🕘 Последние события</b>\n\nСобытий пока нет."
        lines = ["<b>🕘 Последние события архива</b>", ""]
        for message, dialog in rows:
            name = html.escape(str(dialog.peer_name or dialog.peer_username or dialog.telegram_chat_id))
            content = html.escape((message.text or message.caption or "[медиа]").strip())
            if len(content) > 90:
                content = content[:90] + "…"
            icon = "🗑" if message.is_deleted else "✏️" if message.edited_at else "💬"
            lines.append(f"{icon} <b>{name}</b>: {content}")
        return "\n\n".join(lines)


@router.message(Command("dialogs"))
async def dialogs_command(message: Message) -> None:
    if message.from_user:
        await message.answer(await dialogs_text(message.from_user.id), reply_markup=archive_keyboard())


@router.message(Command("recent"))
async def recent_command(message: Message) -> None:
    if message.from_user:
        await message.answer(await recent_text(message.from_user.id), reply_markup=archive_keyboard())


@router.callback_query(F.data == "user:dialogs")
async def dialogs_callback(callback: CallbackQuery) -> None:
    if callback.message:
        await callback.message.answer(await dialogs_text(callback.from_user.id), reply_markup=archive_keyboard())
    await callback.answer()


@router.callback_query(F.data == "user:recent")
async def recent_callback(callback: CallbackQuery) -> None:
    if callback.message:
        await callback.message.answer(await recent_text(callback.from_user.id), reply_markup=archive_keyboard())
    await callback.answer()
