from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy import select

from app.bot.access_funnel import send_access_screen
from app.bot.setup import bot
from app.db.models import User
from app.db.session import SessionLocal
from app.services.access_funnel import (
    check_channel_membership,
    get_funnel_config,
    mark_channel_verified,
)

router = Router(name="channel-check-override")


@router.callback_query(F.data == "funnel:check_channel")
async def check_channel_once(callback: CallbackQuery) -> None:
    config = await get_funnel_config()
    if not config.enabled or not config.channel_required:
        await callback.answer("Проверка подписки сейчас отключена.", show_alert=True)
        return

    ok = await check_channel_membership(
        bot,
        user_id=callback.from_user.id,
        channel_id=config.channel_id,
    )
    if not ok:
        await callback.answer(config.subscription_error_text, show_alert=True)
        return

    await mark_channel_verified(callback.from_user.id)
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id))

    await callback.answer(config.subscription_success_text, show_alert=True)
    if callback.message and user:
        # send_access_screen is the single source of the next message. The old
        # handler used to send business_required_text and then call this method,
        # causing the same message to appear twice.
        await send_access_screen(callback.message, user)
