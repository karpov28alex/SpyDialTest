from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from redis import Redis

from app.core.config import get_settings

settings = get_settings()
CONTENT_KEY = "dialog_spy:user_menu_content"
DEFAULT_OFFER_URL = "https://mooncloud.ltd/spy/terms.html#free"


def _menu_config() -> tuple[str, bool]:
    redis = Redis.from_url(settings.redis_url, decode_responses=True, socket_timeout=1)
    try:
        data = redis.hgetall(CONTENT_KEY)
        offer_url = data.get("offer_url") or DEFAULT_OFFER_URL
        show_offer = data.get("show_offer", "1") not in {"0", "false", "False", "off"}
        return offer_url, show_offer
    except Exception:
        return DEFAULT_OFFER_URL, True
    finally:
        redis.close()


def enhanced_user_keyboard() -> InlineKeyboardMarkup:
    offer_url, show_offer = _menu_config()
    rows = [
        [InlineKeyboardButton(text="📱 Открыть Mini App", web_app=WebAppInfo(url=settings.mini_app_url))],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="user:stats")],
        [
            InlineKeyboardButton(text="👤 Профиль", callback_data="user:profile"),
            InlineKeyboardButton(text="⚙️ Настройки", callback_data="user:settings"),
        ],
        [InlineKeyboardButton(text="📖 Инструкция", callback_data="help")],
    ]
    if show_offer:
        rows.append([InlineKeyboardButton(text="📄 Оферта", url=offer_url)])
    return InlineKeyboardMarkup(inline_keyboard=rows)
