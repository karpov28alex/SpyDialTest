from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.handlers import instruction_content, is_admin

router = Router(name="user-experience")


def instruction_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 В профиль", callback_data="user:profile")],
    ])


async def _send_instruction(message: Message) -> None:
    content = await instruction_content()
    for file_id in (content["video1"], content["video2"]):
        if file_id:
            await message.answer_video(file_id, supports_streaming=True)
    await message.answer(content["text"], reply_markup=instruction_keyboard())


@router.message(Command("help"))
async def help_command(message: Message) -> None:
    await _send_instruction(message)


@router.callback_query(F.data == "help")
async def help_callback(callback: CallbackQuery) -> None:
    if callback.message:
        await _send_instruction(callback.message)
    await callback.answer()


KNOWN_COMMANDS = {
    "start", "menu", "profile", "settings", "stats", "help", "app",
    "admin", "admin_id", "admin_add", "admin_remove", "admins",
    "broadcast", "menu_editor", "instruction_text", "instruction_video1",
    "instruction_video2", "instruction_clear", "subscription", "cancel",
}


@router.message(F.text.startswith("/"))
async def silent_unknown_command(message: Message) -> None:
    raw = (message.text or "").split(maxsplit=1)[0]
    command = raw[1:].split("@", 1)[0].lower()
    if command in KNOWN_COMMANDS:
        return
    # Unknown commands are intentionally ignored. Admin-only commands are
    # handled by their dedicated handlers and return an explicit access error.
    return
