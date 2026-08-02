from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command

from app.core.config import get_settings

settings = get_settings()
bot = Bot(settings.telegram_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dispatcher = Dispatcher()

# Specific handlers are registered before generic routers.
from app.bot.channel_gate_middleware import ChannelGateMiddleware  # noqa: E402
from app.bot.menu_editor_handlers import router as menu_editor_router  # noqa: E402
from app.bot.admin_menu_editor_patch import router as admin_menu_editor_router  # noqa: E402
from app.bot.profile_card_handlers import router as profile_card_router  # noqa: E402
from app.bot.statistics_card_v2_handlers import router as statistics_card_router  # noqa: E402
from app.bot.user_experience_handlers import router as user_experience_router  # noqa: E402
from app.bot.archive_handlers import router as archive_router  # noqa: E402
from app.bot.access_funnel import router as access_funnel_router  # noqa: E402
from app.bot.impaya import cancel_command, pay_callback, pay_command  # noqa: E402
from app.bot.subscription import subscription_command  # noqa: E402
from app.bot import profile_card_handlers, user_handlers  # noqa: E402
from app.bot.enhanced_user_menu import enhanced_user_keyboard  # noqa: E402

# Every non-admin interaction must pass a live informational-channel check.
dispatcher.message.outer_middleware(ChannelGateMiddleware())
dispatcher.callback_query.outer_middleware(ChannelGateMiddleware())

# Keep one consistent, dynamically configurable keyboard everywhere.
user_handlers.user_keyboard = enhanced_user_keyboard
profile_card_handlers._profile_keyboard = enhanced_user_keyboard

# Register payment handlers directly on the dispatcher so payment commands and
# callbacks cannot be shadowed by any generic router included below.
dispatcher.message.register(pay_command, Command("pay"))
dispatcher.message.register(cancel_command, Command("cancel"))
dispatcher.message.register(subscription_command, Command("subscription"))
dispatcher.callback_query.register(pay_callback, F.data == "impaya:pay")

# The access funnel is a user-facing router. It must be attached directly so
# /start and channel-verification callbacks work for ordinary users.
dispatcher.include_router(access_funnel_router)
dispatcher.include_router(menu_editor_router)
dispatcher.include_router(admin_menu_editor_router)
dispatcher.include_router(statistics_card_router)
dispatcher.include_router(profile_card_router)
dispatcher.include_router(user_experience_router)
dispatcher.include_router(archive_router)
dispatcher.include_router(user_handlers.router)
