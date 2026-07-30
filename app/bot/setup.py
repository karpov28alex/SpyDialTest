from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.core.config import get_settings

settings = get_settings()
bot = Bot(settings.telegram_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dispatcher = Dispatcher()

# User-facing commands and callbacks are registered first so /start, /profile,
# /settings, /subscription and /cancel work consistently in the regular bot chat.
from app.bot.user_handlers import router as user_router  # noqa: E402

dispatcher.include_router(user_router)
