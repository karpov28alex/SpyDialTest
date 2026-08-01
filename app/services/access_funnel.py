from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from redis.asyncio import Redis

from app.core.config import get_settings
from app.db.models import User
from app.services.access import has_access

settings = get_settings()
CONFIG_KEY = "phantom:access_funnel:config"
CHANNEL_VERIFIED_PREFIX = "phantom:access_funnel:channel_verified:"


@dataclass(slots=True)
class FunnelConfig:
    enabled: bool = True
    channel_required: bool = True
    channel_id: str = ""
    channel_url: str = ""
    channel_title: str = "Официальный канал Phantom"
    subscription_text: str = (
        "<b>Перед использованием Phantom подпишитесь на наш информационный канал.</b>\n\n"
        "После подписки нажмите кнопку «Проверить подписку»."
    )
    subscription_error_text: str = (
        "Подписка пока не найдена. Подпишитесь на канал и повторите проверку."
    )
    subscription_success_text: str = (
        "✅ Подписка подтверждена. Ваш бесплатный доступ активирован."
    )
    referral_required: bool = True
    referral_text: str = (
        "Бесплатный период завершён. Чтобы продолжить пользоваться Phantom, "
        "пригласите друга или оплатите доступ."
    )
    referral_started_text: str = (
        "👀 По вашей ссылке перешёл новый пользователь.\n\n"
        "Бонус будет начислен после того, как он подпишется на канал, "
        "подключит Telegram Business и начнёт пользоваться Phantom."
    )
    referral_bonus_success_text: str = (
        "🎉 <b>Друг успешно подключил Phantom!</b>\n\n"
        "Он выполнил условия реферальной программы.\n\n"
        "🎁 Вы получаете <b>+{days} дня полного доступа без ограничений</b>."
    )
    referral_share_text: str = (
        "👻 Пользуюсь Phantom — он сохраняет удалённые и изменённые сообщения, "
        "одноразовые фото и видео и собирает приватный архив Telegram Business. "
        "Попробуй бесплатно по моей ссылке:"
    )
    payment_required_text: str = (
        "Бонусный доступ завершён. Для дальнейшего использования необходимо оплатить подписку."
    )
    payment_button_text: str = "💳 Оплатить Phantom"
    payment_url: str = "https://game.hidenow.su/app?screen=subscription"
    redact_expired_notifications: bool = True
    redacted_actor: str = "********"
    redacted_content: str = "************************"


def redis_client() -> Redis:
    return Redis.from_url(settings.redis_url, decode_responses=True)


async def get_funnel_config(redis: Redis | None = None) -> FunnelConfig:
    own = redis is None
    redis = redis or redis_client()
    try:
        data = await redis.hgetall(CONFIG_KEY)
        defaults = asdict(FunnelConfig())
        if not data:
            return FunnelConfig()
        values: dict[str, Any] = {}
        for key, default in defaults.items():
            raw = data.get(key)
            if raw is None:
                values[key] = default
            elif isinstance(default, bool):
                values[key] = raw.lower() in {"1", "true", "yes", "on"}
            else:
                values[key] = raw
        return FunnelConfig(**values)
    finally:
        if own:
            await redis.aclose()


async def save_funnel_config(values: dict[str, Any], redis: Redis | None = None) -> FunnelConfig:
    own = redis is None
    redis = redis or redis_client()
    try:
        current = asdict(await get_funnel_config(redis))
        allowed = set(current)
        for key, value in values.items():
            if key not in allowed or value is None:
                continue
            if isinstance(current[key], bool):
                if isinstance(value, str):
                    current[key] = value.lower() in {"1", "true", "yes", "on"}
                else:
                    current[key] = bool(value)
            else:
                current[key] = str(value).strip()
        await redis.hset(
            CONFIG_KEY,
            mapping={
                key: ("1" if value is True else "0" if value is False else str(value))
                for key, value in current.items()
            },
        )
        return FunnelConfig(**current)
    finally:
        if own:
            await redis.aclose()


async def channel_verified(user_id: int, redis: Redis | None = None) -> bool:
    own = redis is None
    redis = redis or redis_client()
    try:
        return bool(await redis.get(f"{CHANNEL_VERIFIED_PREFIX}{user_id}"))
    finally:
        if own:
            await redis.aclose()


async def mark_channel_verified(user_id: int, redis: Redis | None = None) -> None:
    own = redis is None
    redis = redis or redis_client()
    try:
        await redis.set(
            f"{CHANNEL_VERIFIED_PREFIX}{user_id}",
            datetime.now(UTC).isoformat(),
            ex=900,
        )
    finally:
        if own:
            await redis.aclose()


async def clear_channel_verified(user_id: int, redis: Redis | None = None) -> None:
    own = redis is None
    redis = redis or redis_client()
    try:
        await redis.delete(f"{CHANNEL_VERIFIED_PREFIX}{user_id}")
    finally:
        if own:
            await redis.aclose()


async def check_channel_membership(bot: Bot, *, user_id: int, channel_id: str) -> bool:
    channel_id = channel_id.strip()
    if not channel_id:
        return False
    chat: int | str
    try:
        chat = int(channel_id)
    except ValueError:
        chat = channel_id if channel_id.startswith("@") else f"@{channel_id}"
    try:
        member = await bot.get_chat_member(chat, user_id)
    except Exception:
        return False
    return member.status in {
        ChatMemberStatus.CREATOR,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.RESTRICTED,
    }


async def channel_gate_passed(bot: Bot, *, user_id: int, config: FunnelConfig | None = None) -> bool:
    config = config or await get_funnel_config()
    if not config.enabled or not config.channel_required:
        return True
    ok = await check_channel_membership(bot, user_id=user_id, channel_id=config.channel_id)
    if ok:
        await mark_channel_verified(user_id)
    else:
        await clear_channel_verified(user_id)
    return ok


def notification_is_redacted(user: User, config: FunnelConfig) -> bool:
    return bool(
        config.enabled
        and config.redact_expired_notifications
        and not has_access(user)
    )


def serialize_config(config: FunnelConfig) -> dict[str, Any]:
    return asdict(config)
