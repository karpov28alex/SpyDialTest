from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.core.config import get_settings

settings = get_settings()
bot = Bot(settings.telegram_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dispatcher = Dispatcher()

# Editors are registered before generic user handlers so FSM input and exact
# admin callbacks are handled deterministically.
from app.bot.menu_editor_handlers import router as menu_editor_router  # noqa: E402
from app.bot.admin_menu_editor_patch import router as admin_menu_editor_router  # noqa: E402
from app.bot.profile_card_handlers import router as profile_card_router  # noqa: E402
from app.bot.user_handlers import router as user_router  # noqa: E402

dispatcher.include_router(menu_editor_router)
dispatcher.include_router(admin_menu_editor_router)
dispatcher.include_router(profile_card_router)
dispatcher.include_router(user_router)
