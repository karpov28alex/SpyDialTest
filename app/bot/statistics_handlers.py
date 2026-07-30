from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app.bot.profile_card_handlers import _send_profile

router = Router(name="user-statistics")


@router.message(Command("stats"))
async def statistics_command(message: Message) -> None:
    if message.from_user:
        await _send_profile(message, message.from_user.id)


@router.callback_query(F.data == "user:stats")
async def statistics_callback(callback: CallbackQuery) -> None:
    if callback.message:
        await _send_profile(callback.message, callback.from_user.id)
    await callback.answer()
