from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.core.config import get_settings

settings = get_settings()
bot = Bot(settings.telegram_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dispatcher = Dispatcher()

# The dedicated profile-card router is registered first so /profile and the
# profile button return the live visual statistics card instead of duplicate text.
from app.bot.profile_card_handlers import router as profile_card_router  # noqa: E402
from app.bot.user_handlers import router as user_router  # noqa: E402

dispatcher.include_router(profile_card_router)
dispatcher.include_router(user_router)
