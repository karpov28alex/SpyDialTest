from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import structlog
from aiogram import F, Router
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, Message
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import func, select

from app.bot.admin_console import (
    BROADCAST_HISTORY_KEY,
    CAMPAIGNS_KEY,
    CAMPAIGN_PREFIX,
    CAMPAIGN_USERS_PREFIX,
    back,
    get_state,
    is_admin,
    keyboard,
    redis_client,
    safe_scalar,
    stats,
)
from app.bot.setup import bot
from app.db.models import User
from app.db.session import SessionLocal
from app.services.broadcasts import send_broadcast

router = Router(name="admin_polish")
logger = structlog.get_logger()

FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
)
BOLD_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
)


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = BOLD_FONT_CANDIDATES if bold else FONT_CANDIDATES
    for candidate in candidates:
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size=size)
    logger.warning("analytics_cyrillic_font_missing", candidates=candidates)
    return ImageFont.load_default(size=size)


async def _traffic_sources(since: datetime) -> dict[str, int]:
    async with SessionLocal() as session:
        new_users = list(
            (
                await session.scalars(
                    select(User).where(User.registered_at >= since)
                )
            ).all()
        )

    new_ids = {user.telegram_id for user in new_users}
    referral_ids = {
        user.telegram_id for user in new_users if user.referrer_user_id is not None
    }

    advertising_ids: set[int] = set()
    redis = redis_client()
    try:
        codes = await redis.smembers(CAMPAIGNS_KEY)
        for code in codes:
            members = await redis.smembers(f"{CAMPAIGN_USERS_PREFIX}{code}")
            advertising_ids.update(
                int(value) for value in members if str(value).isdigit()
            )
    except Exception:
        logger.exception("analytics_campaign_sources_failed")
    finally:
        await redis.aclose()

    advertising_ids &= new_ids
    # A paid campaign has priority if a user somehow also has a referrer marker.
    referral_ids -= advertising_ids
    organic_ids = new_ids - advertising_ids - referral_ids
    return {
        "ads": len(advertising_ids),
        "referrals": len(referral_ids),
        "organic": len(organic_ids),
    }


async def analytics_data(days: int) -> dict[str, Any]:
    data = await stats(days)
    data.update(await _traffic_sources(datetime.now(UTC) - timedelta(days=days)))
    return data


def analytics_chart(data: dict[str, Any]) -> bytes:
    image = Image.new("RGB", (1200, 820), "#090713")
    draw = ImageDraw.Draw(image)
    body = _font(28)
    small = _font(24)
    title = _font(42, bold=True)
    label_font = _font(27, bold=True)

    draw.rounded_rectangle(
        (35, 35, 1165, 785), 35, fill="#120d22", outline="#7738e6", width=4
    )
    draw.text(
        (75, 68), f"Dialog Spy · сводка за {data['days']} дн.",
        font=title, fill="#ffffff"
    )

    values = [
        ("Живые", data["alive"], "#55d69d"),
        ("Заблокировали", data["blocked"], "#ff668a"),
        ("VIP", data["vip"], "#ad6cff"),
        ("Бесплатные", data["free"], "#718cff"),
    ]
    maximum = max([value for _, value, _ in values] + [1])
    y = 165
    for label, value, color in values:
        draw.text((80, y), label, font=label_font, fill="#ffffff")
        draw.rounded_rectangle((380, y + 3, 1010, y + 42), 18, fill="#28203d")
        width = int(630 * value / maximum)
        if width:
            draw.rounded_rectangle((380, y + 3, 380 + width, y + 42), 18, fill=color)
        draw.text((1040, y), str(value), font=body, fill="#ffffff")
        y += 88

    draw.line((75, 535, 1125, 535), fill="#33264d", width=2)
    draw.text((80, 565), "Источники новых пользователей", font=label_font, fill="#d8b9ff")
    draw.text(
        (80, 620),
        f"Реклама: {data['ads']}   ·   Приглашения: {data['referrals']}   ·   Саморост: {data['organic']}",
        font=body,
        fill="#ffffff",
    )
    draw.text(
        (80, 700),
        f"Новые: {data['new']}   ·   Доход: {Decimal(data['revenue']):,.2f} ₽   ·   Сообщения: {data['messages']}",
        font=small,
        fill="#d9cdf2",
    )

    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


async def send_polished_analytics(message: Message, days: int) -> None:
    data = await analytics_data(days)
    caption = (
        f"<b>📊 Сводка за {days} дн.</b>\n\n"
        f"Новых: <b>{data['new']}</b>\n"
        f"Живых: <b>{data['alive']}</b> · заблокировали: <b>{data['blocked']}</b>\n"
        f"VIP: <b>{data['vip']}</b> · бесплатных: <b>{data['free']}</b>\n\n"
        "<b>Источники новых пользователей</b>\n"
        f"📣 Реклама: <b>{data['ads']}</b>\n"
        f"🤝 Приглашения: <b>{data['referrals']}</b>\n"
        f"🌱 Саморост: <b>{data['organic']}</b>\n\n"
        f"Доход: <b>{Decimal(data['revenue']):,.2f} ₽</b>\n"
        f"Сообщений: <b>{data['messages']}</b>\n"
        f"Business-подключений: <b>{data['connections']}</b>"
    )
    ranges = keyboard([
        [
            InlineKeyboardButton(text="Сегодня", callback_data="crm:range:1"),
            InlineKeyboardButton(text="7 дней", callback_data="crm:range:7"),
            InlineKeyboardButton(text="30 дней", callback_data="crm:range:30"),
        ],
        [InlineKeyboardButton(text="🗓 Свой период", callback_data="crm:range:custom")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="crm:home")],
    ])
    await message.answer_photo(
        BufferedInputFile(analytics_chart(data), filename=f"analytics-{days}.png"),
        caption=caption,
        reply_markup=ranges,
    )


@router.callback_query(F.data == "crm:analytics")
async def analytics_callback(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    if callback.message:
        await send_polished_analytics(callback.message, 7)
    await callback.answer()


@router.callback_query(F.data.in_({"crm:range:1", "crm:range:7", "crm:range:30"}))
async def range_callback(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    if callback.message:
        await send_polished_analytics(callback.message, int((callback.data or "").rsplit(":", 1)[1]))
    await callback.answer()


@router.callback_query(F.data == "crm:broadcast_preview")
async def broadcast_preview_callback(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    message = callback.message
    if not message:
        await callback.answer()
        return
    state = await get_state(callback.from_user.id) or {}
    if not state.get("text") and not state.get("media"):
        await callback.answer("Добавьте контент", show_alert=True)
        return

    # Send the exact same payload as the worker will send to users. This makes
    # captions, albums and URL buttons visually identical to the final mailing.
    await message.answer("<b>👁 Предпросмотр рассылки</b>\nНиже сообщение в итоговом виде:")
    await send_broadcast(
        bot,
        {
            "telegram_id": message.chat.id,
            "text": state.get("text") or "",
            "media": state.get("media") or [],
            "buttons": state.get("buttons") or [],
        },
    )

    audience_names = {"all": "все", "vip": "VIP", "free": "бесплатные"}
    audience = state.get("audience", "all")
    count_stmt = select(func.count(User.id)).where(User.blocked_bot_at.is_(None))
    from app.db.models import SubscriptionStatus
    if audience == "vip":
        count_stmt = count_stmt.where(
            User.subscription_status.in_([SubscriptionStatus.vip, SubscriptionStatus.active])
        )
    elif audience == "free":
        count_stmt = count_stmt.where(
            User.subscription_status.not_in([SubscriptionStatus.vip, SubscriptionStatus.active])
        )
    count = int(await safe_scalar(count_stmt))

    controls = keyboard([
        [InlineKeyboardButton(text=f"🚀 Отправить · {count}", callback_data="crm:broadcast_send")],
        [InlineKeyboardButton(text="💾 Сохранить черновик", callback_data="crm:broadcast_save")],
        [InlineKeyboardButton(text="✖️ Отмена", callback_data="crm:cancel")],
    ])
    await message.answer(
        "<b>Параметры рассылки</b>\n\n"
        f"Аудитория: <b>{audience_names.get(audience, audience)}</b>\n"
        f"Получателей: <b>{count}</b>\n"
        f"Медиа: <b>{len(state.get('media') or [])}</b>\n"
        f"Кнопок: <b>{len(state.get('buttons') or [])}</b>",
        reply_markup=controls,
    )
    await callback.answer()
