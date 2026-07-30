from __future__ import annotations

import base64
import logging
import math
import re
import statistics
from collections import defaultdict
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps
from sqlalchemy import select

from app.bot.setup import bot
from app.core.config import get_settings
from app.db.models import BusinessConnection, Dialog, Media, Message as DbMessage, User
from app.db.session import SessionLocal

router = Router(name="statistics-card")
settings = get_settings()
logger = logging.getLogger(__name__)
LOGO_B64_PATH = Path("app/static/miniapp/phantom-logo.b64")
OFFER_URL = "https://mooncloud.ltd/spy/terms.html#free"


def _menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📱 Открыть Dialog Spy", web_app=WebAppInfo(url=settings.mini_app_url))],
            [InlineKeyboardButton(text="👤 Профиль", callback_data="user:profile")],
            [InlineKeyboardButton(text="⚙️ Настройки", callback_data="user:settings")],
            [InlineKeyboardButton(text="📖 Инструкция", callback_data="help")],
            [InlineKeyboardButton(text="📄 Оферта", url=OFFER_URL)],
        ]
    )


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    )
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    raise RuntimeError("Cyrillic font is not installed")


def _clean(value: str | None, fallback: str = "Без имени", limit: int = 26) -> str:
    text = re.sub(r"\s+", " ", (value or "").strip())
    return (text or fallback)[:limit]


def _load_logo(size: tuple[int, int]) -> Image.Image:
    encoded = re.sub(r"\s+", "", LOGO_B64_PATH.read_text(encoding="utf-8"))
    raw = base64.b64decode(encoded, validate=True)
    logo = Image.open(BytesIO(raw)).convert("RGB")
    return ImageOps.fit(logo, size, method=Image.Resampling.LANCZOS)


def _format_number(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def _duration_text(seconds: float | None) -> str:
    if seconds is None:
        return "Недостаточно данных"
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} сек."
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} мин. {seconds} сек."
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ч. {minutes} мин."


def _dialog_payload(dialog: Dialog) -> dict[str, Any]:
    return {
        "id": dialog.id,
        "telegram_id": dialog.peer_telegram_id or dialog.telegram_chat_id,
        "name": _clean(dialog.peer_name or dialog.peer_username, str(dialog.telegram_chat_id)),
        "username": f"@{dialog.peer_username.lstrip('@')}" if dialog.peer_username else "",
        "avatar_hint": dialog.avatar,
    }


async def _collect_stats(telegram_id: int) -> dict[str, Any] | None:
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            return None
        dialogs = list((await session.scalars(select(Dialog).where(Dialog.owner_user_id == user.id))).all())
        if not dialogs:
            return {
                "owner": user,
                "connected": False,
                "dialogs": [],
                "totals": {"dialogs": 0, "messages": 0, "edited": 0, "deleted": 0, "protected": 0},
                "leaders": {},
                "insights": ["Недостаточно данных для анализа."],
            }

        dialog_ids = [dialog.id for dialog in dialogs]
        messages = list((await session.scalars(select(DbMessage).where(DbMessage.dialog_id.in_(dialog_ids)).order_by(DbMessage.sent_at))).all())
        media = list(
            (
                await session.execute(
                    select(Media, DbMessage.dialog_id)
                    .join(DbMessage, DbMessage.id == Media.message_id)
                    .where(DbMessage.dialog_id.in_(dialog_ids))
                )
            ).all()
        )
        connected = bool(
            await session.scalar(
                select(BusinessConnection.id).where(
                    BusinessConnection.owner_user_id == user.id,
                    BusinessConnection.is_active.is_(True),
                ).limit(1)
            )
        )

    dialog_map = {dialog.id: dialog for dialog in dialogs}
    msg_counts: dict[int, int] = defaultdict(int)
    deleted_counts: dict[int, int] = defaultdict(int)
    media_counts: dict[int, int] = defaultdict(int)
    protected_counts: dict[int, int] = defaultdict(int)
    sent_ranges: dict[int, list[datetime]] = defaultdict(list)
    response_times: dict[int, list[float]] = defaultdict(list)
    previous_by_dialog: dict[int, DbMessage] = {}
    hour_counts: dict[int, int] = defaultdict(int)

    for message in messages:
        msg_counts[message.dialog_id] += 1
        if message.is_deleted:
            deleted_counts[message.dialog_id] += 1
        if message.sent_at:
            sent_ranges[message.dialog_id].append(message.sent_at)
            hour_counts[message.sent_at.hour] += 1
        previous = previous_by_dialog.get(message.dialog_id)
        if (
            previous is not None
            and previous.sent_at
            and message.sent_at
            and previous.direction != message.direction
            and message.direction == "outgoing"
        ):
            delta = (message.sent_at - previous.sent_at).total_seconds()
            if 0 <= delta <= 86400:
                response_times[message.dialog_id].append(delta)
        previous_by_dialog[message.dialog_id] = message

    for media_item, dialog_id in media:
        media_counts[dialog_id] += 1
        if media_item.is_protected:
            protected_counts[dialog_id] += 1

    def leader(counts: dict[int, int]) -> dict[str, Any] | None:
        if not counts:
            return None
        dialog_id, value = max(counts.items(), key=lambda item: item[1])
        if value <= 0:
            return None
        result = _dialog_payload(dialog_map[dialog_id])
        result["value"] = value
        return result

    active = leader(msg_counts)
    medial = leader(media_counts)
    mysterious = leader(protected_counts)
    deleting = leader(deleted_counts)

    longest_dialog: dict[str, Any] | None = None
    longest_days = -1
    for dialog_id, dates in sent_ranges.items():
        if not dates:
            continue
        days = max(0, (max(dates) - min(dates)).days)
        if days > longest_days:
            longest_days = days
            longest_dialog = _dialog_payload(dialog_map[dialog_id])
            longest_dialog.update({"days": days, "started": min(dates), "value": msg_counts[dialog_id]})

    fastest: dict[str, Any] | None = None
    fastest_seconds = math.inf
    for dialog_id, values in response_times.items():
        if not values:
            continue
        average = statistics.mean(values)
        if average < fastest_seconds:
            fastest_seconds = average
            fastest = _dialog_payload(dialog_map[dialog_id])
            fastest["seconds"] = average

    peak_hour = max(hour_counts.items(), key=lambda item: item[1])[0] if hour_counts else None
    insights: list[str] = []
    if active:
        insights.append(f"💜 Чаще всего вы общаетесь с {active['name']} — {_format_number(active['value'])} сообщений.")
    if medial:
        insights.append(f"📸 Больше всего медиа в диалоге с {medial['name']} — {_format_number(medial['value'])} файлов.")
    if deleting:
        insights.append(f"🗑 Чаще всего сообщения удаляет {deleting['name']} — {_format_number(deleting['value'])}.")
    if fastest:
        insights.append(f"⚡ Самый быстрый диалог — {fastest['name']}, средний ответ {_duration_text(fastest['seconds'])}.")
    if peak_hour is not None:
        period = "ночью" if peak_hour < 6 else "утром" if peak_hour < 12 else "днём" if peak_hour < 18 else "вечером"
        insights.append(f"🔥 Наибольшая активность происходит {period}, около {peak_hour:02d}:00.")
    if not insights:
        insights.append("Недостаточно данных для персональных выводов.")

    return {
        "owner": user,
        "connected": connected,
        "dialogs": dialogs,
        "totals": {
            "dialogs": len(dialogs),
            "messages": len(messages),
            "edited": sum(1 for item in messages if item.edited_at is not None),
            "deleted": sum(1 for item in messages if item.is_deleted),
            "protected": sum(1 for item, _ in media if item.is_protected),
        },
        "leaders": {
            "active": active,
            "media": medial,
            "protected": mysterious,
            "deleted": deleting,
            "longest": longest_dialog,
            "fastest": fastest,
        },
        "insights": insights[:5],
    }


async def _avatar(peer_id: int | None, hint: str | None) -> bytes | None:
    if peer_id:
        try:
            photos = await bot.get_user_profile_photos(peer_id, limit=1)
            if photos.total_count and photos.photos:
                buffer = BytesIO()
                await bot.download(photos.photos[0][-1].file_id, destination=buffer)
                return buffer.getvalue()
        except Exception:
            logger.debug("statistics_avatar_unavailable", exc_info=True, extra={"peer_id": peer_id})
    if hint and hint.startswith("data:image") and "," in hint:
        try:
            return base64.b64decode(hint.split(",", 1)[1])
        except Exception:
            return None
    return None


async def _leader_avatars(stats: dict[str, Any]) -> dict[str, bytes | None]:
    result: dict[str, bytes | None] = {}
    for key in ("active", "media", "protected", "deleted"):
        item = stats["leaders"].get(key)
        result[key] = await _avatar(item.get("telegram_id"), item.get("avatar_hint")) if item else None
    return result


def _glass(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], radius: int = 26, outline: str = "#6630a1") -> None:
    draw.rounded_rectangle(box, radius=radius, fill="#120b22", outline=outline, width=2)


def _circle_avatar(image: Image.Image, raw: bytes | None, center: tuple[int, int], size: int, initials: str) -> None:
    x, y = center
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
    if raw:
        try:
            avatar = Image.open(BytesIO(raw)).convert("RGB")
            avatar = ImageOps.fit(avatar, (size, size), method=Image.Resampling.LANCZOS)
        except Exception:
            raw = None
    if not raw:
        avatar = Image.new("RGB", (size, size), "#2a1645")
        d = ImageDraw.Draw(avatar)
        label = (initials or "?")[:1].upper()
        bbox = d.textbbox((0, 0), label, font=_font(size // 2, True))
        d.text(((size - (bbox[2] - bbox[0])) / 2, (size - (bbox[3] - bbox[1])) / 2 - 5), label, font=_font(size // 2, True), fill="#d6a8ff")
    glow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((x - size // 2 - 8, y - size // 2 - 8, x + size // 2 + 8, y + size // 2 + 8), fill="#7f26df")
    glow = glow.filter(ImageFilter.GaussianBlur(10))
    image.paste(glow, (0, 0), glow)
    image.paste(avatar, (x - size // 2, y - size // 2), mask)
    ImageDraw.Draw(image).ellipse((x - size // 2, y - size // 2, x + size // 2, y + size // 2), outline="#ad4cff", width=4)


def _render(stats: dict[str, Any], avatars: dict[str, bytes | None]) -> bytes:
    width = height = 1080
    image = Image.new("RGB", (width, height), "#05030d")
    glow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((-180, -220, 620, 580), fill="#391064")
    gd.ellipse((650, 500, 1280, 1160), fill="#240747")
    glow = glow.filter(ImageFilter.GaussianBlur(130))
    image.paste(glow, (0, 0), glow)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((24, 24, 1056, 1056), radius=36, outline="#9e37ff", width=3)

    logo = _load_logo((145, 145))
    image.paste(logo, (55, 42))
    draw.text((222, 55), "PHANTOM", font=_font(47, True), fill="#ffffff")
    draw.text((224, 112), "ПЕРСОНАЛЬНАЯ СТАТИСТИКА", font=_font(21, True), fill="#a855f7")
    owner: User = stats["owner"]
    name = _clean(" ".join(part for part in (owner.first_name, owner.last_name) if part), "Пользователь", 34)
    username = f"@{owner.username}" if owner.username else f"ID {owner.telegram_id}"
    draw.text((224, 149), f"{name}  ·  {username}", font=_font(19), fill="#cbbbd8")
    now = datetime.now(UTC)
    draw.rounded_rectangle((820, 58, 1019, 151), radius=25, fill="#130b23", outline="#4b226d", width=2)
    draw.text((852, 77), now.strftime("Сегодня · %H:%M"), font=_font(20, True), fill="#ffffff")
    status = "TELEGRAM BUSINESS ПОДКЛЮЧЁН" if stats["connected"] else "TELEGRAM BUSINESS НЕ ПОДКЛЮЧЁН"
    color = "#4ee29a" if stats["connected"] else "#ff6b86"
    draw.rounded_rectangle((222, 182, 615, 218), radius=18, fill="#120b22", outline="#43205f", width=2)
    draw.ellipse((238, 193, 252, 207), fill=color)
    draw.text((266, 188), status, font=_font(16, True), fill=color)

    totals = stats["totals"]
    metric_items = [
        ("ДИАЛОГИ", totals["dialogs"]),
        ("СООБЩЕНИЯ", totals["messages"]),
        ("ИЗМЕНЕНИЯ", totals["edited"]),
        ("УДАЛЕНИЯ", totals["deleted"]),
        ("СКРЫТЫЕ МЕДИА", totals["protected"]),
    ]
    _glass(draw, (54, 245, 1026, 408), radius=30)
    for index, (label, value) in enumerate(metric_items):
        x = 76 + index * 190
        if index:
            draw.line((x - 20, 278, x - 20, 376), fill="#3d2455", width=2)
        draw.text((x, 276), _format_number(value), font=_font(35, True), fill="#ffffff")
        draw.text((x, 338), label, font=_font(14, True), fill="#9e91aa")

    leader_specs = [
        ("active", "🏆 БОЛЬШЕ ВСЕГО ОБЩЕНИЯ", "сообщений"),
        ("media", "📸 БОЛЬШЕ ВСЕГО МЕДИА", "медиа"),
        ("protected", "👻 БОЛЬШЕ СКРЫТЫХ МЕДИА", "файлов"),
        ("deleted", "🗑 БОЛЬШЕ ВСЕГО УДАЛЕНИЙ", "удалений"),
    ]
    for index, (key, title, unit) in enumerate(leader_specs):
        col = index % 2
        row = index // 2
        x1 = 54 + col * 492
        y1 = 438 + row * 202
        x2 = x1 + 466
        y2 = y1 + 180
        _glass(draw, (x1, y1, x2, y2), radius=26)
        draw.text((x1 + 22, y1 + 18), title, font=_font(15, True), fill="#b266ff")
        item = stats["leaders"].get(key)
        if not item:
            draw.text((x1 + 22, y1 + 78), "Недостаточно данных", font=_font(22, True), fill="#ffffff")
            continue
        _circle_avatar(image, avatars.get(key), (x1 + 82, y1 + 108), 88, item["name"])
        draw.text((x1 + 145, y1 + 63), item["name"], font=_font(23, True), fill="#ffffff")
        draw.text((x1 + 145, y1 + 96), item["username"] or "Без username", font=_font(17), fill="#a99bb5")
        draw.text((x1 + 145, y1 + 128), f"{_format_number(item['value'])} {unit}", font=_font(19, True), fill="#c270ff")

    _glass(draw, (54, 854, 1026, 1015), radius=28)
    draw.text((78, 875), "ИНТЕРЕСНЫЕ ФАКТЫ", font=_font(19, True), fill="#b464ff")
    insights = stats["insights"][:4]
    for index, text in enumerate(insights):
        clean = _clean(text, "Недостаточно данных", 92)
        draw.text((78, 912 + index * 25), clean, font=_font(15), fill="#d9d0df")
    longest = stats["leaders"].get("longest")
    fastest = stats["leaders"].get("fastest")
    if longest:
        draw.text((610, 875), f"Самый долгий диалог: {longest['days']} дн.", font=_font(16, True), fill="#ffffff")
        draw.text((610, 903), f"{longest['name']} · {_format_number(longest['value'])} сообщений", font=_font(14), fill="#a99bb5")
    if fastest:
        draw.text((610, 941), f"Самый быстрый ответ: {_duration_text(fastest['seconds'])}", font=_font(16, True), fill="#ffffff")
        draw.text((610, 969), fastest["name"], font=_font(14), fill="#a99bb5")
    draw.text((760, 1026), "Статистика сформирована Phantom Spy", font=_font(13), fill="#766b82")

    output = BytesIO()
    image.save(output, "PNG", optimize=True)
    return output.getvalue()


async def _send(target: Message, telegram_id: int) -> None:
    await target.answer("⏳ Собираю актуальную статистику и вычисляю лидеров…")
    stats = await _collect_stats(telegram_id)
    if stats is None:
        await target.answer("Профиль ещё не создан. Отправьте /start.", reply_markup=_menu())
        return
    try:
        avatars = await _leader_avatars(stats)
        card = BufferedInputFile(_render(stats, avatars), filename="phantom-statistics.png")
        await target.answer_photo(
            card,
            caption=(
                "<b>📊 Персональная статистика Phantom</b>\n\n"
                "Карточка сформирована заново по актуальным данным вашего архива."
            ),
            reply_markup=_menu(),
        )
    except Exception:
        logger.exception("statistics_card_failed", extra={"telegram_id": telegram_id})
        totals = stats["totals"]
        await target.answer(
            "<b>📊 Персональная статистика Phantom</b>\n\n"
            f"Диалогов: <b>{totals['dialogs']}</b>\n"
            f"Сообщений: <b>{totals['messages']}</b>\n"
            f"Изменений: <b>{totals['edited']}</b>\n"
            f"Удалений: <b>{totals['deleted']}</b>\n"
            f"Скрытых медиа: <b>{totals['protected']}</b>",
            reply_markup=_menu(),
        )


@router.message(Command("stats"))
async def statistics_command(message: Message) -> None:
    if message.from_user:
        await _send(message, message.from_user.id)


@router.callback_query(F.data == "user:stats")
async def statistics_callback(callback: CallbackQuery) -> None:
    await callback.answer("Формирую статистику…")
    if callback.message:
        await _send(callback.message, callback.from_user.id)
