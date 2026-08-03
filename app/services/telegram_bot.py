from __future__ import annotations

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode

from app.core.config import Settings


def build_bot(settings: Settings) -> Bot:
    """Create a bot client for either Telegram cloud API or a local Bot API server."""
    session = None
    if settings.telegram_api_base_url:
        session = AiohttpSession(
            api=TelegramAPIServer.from_base(
                settings.telegram_api_base_url,
                is_local=settings.telegram_api_is_local,
            )
        )
    return Bot(
        settings.telegram_bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
