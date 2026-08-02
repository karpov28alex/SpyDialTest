from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, TelegramObject

from app.bot.handlers import is_admin
from app.services.access_funnel import channel_gate_passed, get_funnel_config


def _subscription_keyboard(channel_url: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if channel_url.startswith("https://"):
        rows.append([InlineKeyboardButton(text="📢 Подписаться на канал", url=channel_url)])
    rows.append([InlineKeyboardButton(text="✅ Проверить подписку", callback_data="funnel:check_channel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


class ChannelGateMiddleware(BaseMiddleware):
    """Require a live channel membership check for every user interaction.

    Administrators and the funnel's own verification/admin callbacks are exempt.
    /start is allowed through so registration and referral attribution still run;
    its handler performs the same channel check before showing the user menu.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        user_id = getattr(user, "id", None)
        if user_id is None or await is_admin(user_id):
            return await handler(event, data)

        if isinstance(event, Message):
            text = (event.text or "").strip()
            if text.startswith("/start"):
                return await handler(event, data)
        elif isinstance(event, CallbackQuery):
            callback_data = event.data or ""
            if callback_data == "funnel:check_channel" or callback_data.startswith("funnel:admin"):
                return await handler(event, data)

        config = await get_funnel_config()
        if not config.enabled or not config.channel_required:
            return await handler(event, data)

        bot = data.get("bot")
        if bot is not None and await channel_gate_passed(bot, user_id=user_id, config=config):
            return await handler(event, data)

        markup = _subscription_keyboard(config.channel_url)
        if isinstance(event, CallbackQuery):
            await event.answer("Сначала подпишитесь на информационный канал.", show_alert=True)
            if event.message:
                await event.message.answer(config.subscription_text, reply_markup=markup)
        elif isinstance(event, Message):
            await event.answer(config.subscription_text, reply_markup=markup)
        return None
