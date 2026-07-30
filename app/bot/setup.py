from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.core.config import get_settings

settings = get_settings()
bot = Bot(settings.telegram_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dispatcher = Dispatcher()

# Specific routers are registered before the generic user router so exact
# callbacks and FSM input are handled deterministically.
from app.bot.menu_editor_handlers import router as menu_editor_router  # noqa: E402
from app.bot.admin_menu_editor_patch import router as admin_menu_editor_router  # noqa: E402
from app.bot.profile_card_handlers import router as profile_card_router  # noqa: E402
from app.bot.archive_handlers import router as archive_router  # noqa: E402
from app.bot import user_handlers  # noqa: E402
from app.bot.enhanced_user_menu import enhanced_user_keyboard  # noqa: E402

# Existing handlers resolve this global at runtime, so all /start, /menu,
# profile and settings responses receive the expanded keyboard.
user_handlers.user_keyboard = enhanced_user_keyboard

dispatcher.include_router(menu_editor_router)
dispatcher.include_router(admin_menu_editor_router)
dispatcher.include_router(profile_card_router)
dispatcher.include_router(archive_router)
dispatcher.include_router(user_handlers.router)
