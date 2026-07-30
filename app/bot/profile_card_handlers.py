from __future__ import annotations

import base64
import logging
import re
import unicodedata
from io import BytesIO
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps
from sqlalchemy import func, select

from app.core.config import get_settings
from app.db.models import BusinessConnection, Dialog, Media, Message as DbMessage, User
from app.db.session import SessionLocal

router = Router(name="profile-card")
settings = get_settings()
logger = logging.getLogger(__name__)
LOGO_B64_PATH = Path("app/static/miniapp/phantom-logo.b64")
OFFER_URL = "https://mooncloud.ltd/spy/terms.html#free"


def _profile_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📱 Открыть Dialog Spy", web_app=WebAppInfo(url=settings.mini_app_url))],
            [
                InlineKeyboardButton(text="👤 Профиль", callback_data="user:profile"),
                InlineKeyboardButton(text="⚙️ Настройки", callback_data="user:settings"),
            ],
            [InlineKeyboardButton(text="📖 Инструкция", callback_data="help")],
            [InlineKeyboardButton(text="📄 Оферта", url=OFFER_URL)],
        ]
    )


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
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
    raise RuntimeError("Cyrillic font is not installed in the container")


def _clean_text(value: str | None, fallback: str) -> str:
    text = unicodedata.normalize("NFKC", value or "").strip()
    text = text.replace("\ufe0f", "").replace("\u200d", "")
    text = re.sub(r"[^0-9A-Za-zА-Яа-яЁё@._\-() №]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or fallback


def _load_logo() -> Image.Image:
    encoded = re.sub(r"\s+", "", LOGO_B64_PATH.read_text(encoding="utf-8"))
    raw = base64.b64decode(encoded, validate=True)
    logo = Image.open(BytesIO(raw)).convert("RGB")
    return ImageOps.fit(logo, (230, 230), method=Image.Resampling.LANCZOS)


async def _stats(telegram_id: int) -> dict | None:
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            return None
        connected = bool(
            await session.scalar(
                select(func.count(BusinessConnection.id)).where(
                    BusinessConnection.owner_user_id == user.id,
                    BusinessConnection.is_active.is_(True),
                )
            )
        )
        dialogs = int(await session.scalar(select(func.count(Dialog.id)).where(Dialog.owner_user_id == user.id)) or 0)
        base = select(func.count(DbMessage.id)).join(Dialog, Dialog.id == DbMessage.dialog_id).where(Dialog.owner_user_id == user.id)
        messages = int(await session.scalar(base) or 0)
        edited = int(await session.scalar(base.where(DbMessage.edited_at.is_not(None))) or 0)
        deleted = int(await session.scalar(base.where(DbMessage.is_deleted.is_(True))) or 0)
        protected = int(
            await session.scalar(
                select(func.count(Media.id))
                .join(DbMessage, DbMessage.id == Media.message_id)
                .join(Dialog, Dialog.id == DbMessage.dialog_id)
                .where(Dialog.owner_user_id == user.id, Media.is_protected.is_(True))
            )
            or 0
        )
        plain_name = " ".join(part for part in (user.first_name, user.last_name) if part)
        return {
            "name": _clean_text(plain_name, _clean_text(user.username, "Пользователь")),
            "username": f"@{_clean_text(user.username, str(user.telegram_id))}" if user.username else f"Telegram ID {user.telegram_id}",
            "connected": connected,
            "dialogs": dialogs,
            "messages": messages,
            "edited": edited,
            "deleted": deleted,
            "protected": protected,
        }


def _gradient_background(width: int, height: int) -> Image.Image:
    image = Image.new("RGB", (width, height), "#05030d")
    pixels = image.load()
    for y in range(height):
        for x in range(width):
            glow = max(0.0, 1.0 - (((x - 190) ** 2 + (y - 100) ** 2) ** 0.5) / 760)
            pixels[x, y] = (int(5 + 25 * glow), int(3 + 5 * glow), int(13 + 42 * glow))
    return image


def _render_card(data: dict) -> bytes:
    width, height = 1280, 760
    image = _gradient_background(width, height)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((38, 34, 1242, 726), radius=50, fill="#0b0717", outline="#822cff", width=5)
    draw.rounded_rectangle((58, 54, 1222, 706), radius=42, outline="#3c1a68", width=2)

    logo = _load_logo()
    shadow = Image.new("RGBA", logo.size, (0, 0, 0, 0))
    shadow.paste(logo.convert("RGBA"), (0, 0))
    shadow = shadow.filter(ImageFilter.GaussianBlur(15))
    image.paste(shadow, (76, 66), shadow)
    image.paste(logo, (76, 66))

    draw.text((340, 76), "PHANTOM", font=_font(58, True), fill="#ffffff")
    draw.text((342, 148), "ЛИЧНЫЙ ПРОФИЛЬ", font=_font(29, True), fill="#a95aff")
    draw.text((342, 208), data["name"], font=_font(38, True), fill="#ffffff")
    draw.text((344, 260), data["username"], font=_font(25), fill="#958aa8")

    status = "TELEGRAM BUSINESS ПОДКЛЮЧЁН" if data["connected"] else "TELEGRAM BUSINESS НЕ ПОДКЛЮЧЁН"
    status_color = "#51e49b" if data["connected"] else "#ff6d89"
    draw.rounded_rectangle((342, 309, 850, 354), radius=20, fill="#151022", outline="#34214d", width=2)
    draw.ellipse((364, 323, 380, 339), fill=status_color)
    draw.text((398, 316), status, font=_font(20, True), fill=status_color)

    cards = [
        ("ДИАЛОГИ", data["dialogs"]),
        ("СООБЩЕНИЯ", data["messages"]),
        ("ИЗМЕНЕНИЯ", data["edited"]),
        ("УДАЛЕНИЯ", data["deleted"]),
        ("СКРЫТЫЕ МЕДИА", data["protected"]),
    ]
    x_positions = [64, 308, 552, 796, 1040]
    for x, (label, value) in zip(x_positions, cards, strict=True):
        draw.rounded_rectangle((x, 408, x + 212, 620), radius=28, fill="#151022", outline="#4a286f", width=2)
        draw.rounded_rectangle((x + 18, 428, x + 58, 468), radius=12, fill="#7020e8")
        draw.ellipse((x + 30, 440, x + 46, 456), fill="#ffffff")
        draw.text((x + 20, 486), str(value), font=_font(44, True), fill="#ffffff")
        draw.text((x + 20, 554), label, font=_font(19, True), fill="#a89db8")

    engagement = min(100, data["edited"] * 4 + data["deleted"] * 5 + data["protected"] * 8)
    draw.text((70, 654), "АКТИВНОСТЬ АРХИВА", font=_font(19, True), fill="#958aa8")
    draw.rounded_rectangle((306, 657, 1088, 680), radius=12, fill="#27163a")
    fill_width = int(782 * engagement / 100)
    if fill_width > 0:
        draw.rounded_rectangle((306, 657, 306 + fill_width, 680), radius=12, fill="#8d35ff")
    draw.text((1110, 650), f"{engagement}%", font=_font(22, True), fill="#d8bfff")

    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


async def _send_profile(target: Message, telegram_id: int) -> None:
    data = await _stats(telegram_id)
    if data is None:
        await target.answer("Профиль ещё не создан. Отправьте /start.", reply_markup=_profile_keyboard())
        return
    try:
        card = BufferedInputFile(_render_card(data), filename="phantom-profile.png")
        await target.answer_photo(
            card,
            caption=(
                "<b>Ваш профиль Phantom</b>\n\n"
                "Статистика обновляется при каждом открытии. Используйте кнопки ниже для перехода в Mini App, настройки или инструкцию."
            ),
            reply_markup=_profile_keyboard(),
        )
    except Exception:
        logger.exception("profile_card_render_failed", extra={"telegram_id": telegram_id})
        await target.answer(
            "<b>Ваш профиль Phantom</b>\n\n"
            f"Диалогов: <b>{data['dialogs']}</b>\n"
            f"Сообщений: <b>{data['messages']}</b>\n"
            f"Изменений: <b>{data['edited']}</b>\n"
            f"Удалений: <b>{data['deleted']}</b>\n"
            f"Скрытых медиа: <b>{data['protected']}</b>",
            reply_markup=_profile_keyboard(),
        )


@router.message(Command("profile"))
async def profile_command(message: Message) -> None:
    if message.from_user:
        await _send_profile(message, message.from_user.id)


@router.callback_query(F.data == "user:profile")
async def profile_callback(callback: CallbackQuery) -> None:
    if callback.message:
        await _send_profile(callback.message, callback.from_user.id)
    await callback.answer()
