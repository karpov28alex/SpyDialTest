from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from app.core.config import get_settings

settings = get_settings()
OFFER_URL = "https://mooncloud.ltd/spy/terms.html#free"


def enhanced_user_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 Открыть Dialog Spy", web_app=WebAppInfo(url=settings.mini_app_url))],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="user:stats")],
        [
            InlineKeyboardButton(text="👤 Профиль", callback_data="user:profile"),
            InlineKeyboardButton(text="⚙️ Настройки", callback_data="user:settings"),
        ],
        [InlineKeyboardButton(text="📖 Инструкция", callback_data="help")],
        [InlineKeyboardButton(text="📄 Оферта", url=OFFER_URL)],
    ])
