from __future__ import annotations

from io import BytesIO

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import func, select

from app.db.models import BusinessConnection, Dialog, Media, Message as DbMessage, User
from app.db.session import SessionLocal

router = Router(name="profile-card")


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    paths = (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    )
    for path in paths:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


async def _stats(telegram_id: int) -> dict | None:
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            return None
        connected = bool(await session.scalar(
            select(func.count(BusinessConnection.id)).where(
                BusinessConnection.owner_user_id == user.id,
                BusinessConnection.is_active.is_(True),
            )
        ))
        dialogs = int(await session.scalar(select(func.count(Dialog.id)).where(Dialog.owner_user_id == user.id)) or 0)
        base = select(func.count(DbMessage.id)).join(Dialog, Dialog.id == DbMessage.dialog_id).where(Dialog.owner_user_id == user.id)
        messages = int(await session.scalar(base) or 0)
        edited = int(await session.scalar(base.where(DbMessage.edited_at.is_not(None))) or 0)
        deleted = int(await session.scalar(base.where(DbMessage.is_deleted.is_(True))) or 0)
        protected = int(await session.scalar(
            select(func.count(Media.id))
            .join(DbMessage, DbMessage.id == Media.message_id)
            .join(Dialog, Dialog.id == DbMessage.dialog_id)
            .where(Dialog.owner_user_id == user.id, Media.is_protected.is_(True))
        ) or 0)
        name = " ".join(part for part in (user.first_name, user.last_name) if part) or user.username or "Пользователь"
        return {
            "name": name,
            "username": f"@{user.username}" if user.username else f"ID {user.telegram_id}",
            "connected": connected,
            "dialogs": dialogs,
            "messages": messages,
            "edited": edited,
            "deleted": deleted,
            "protected": protected,
        }


def _render_card(data: dict) -> bytes:
    width, height = 1200, 720
    image = Image.new("RGB", (width, height), "#05040d")
    draw = ImageDraw.Draw(image)

    for y in range(height):
        ratio = y / height
        draw.line((0, y, width, y), fill=(8 + int(10 * ratio), 5, 20 + int(18 * ratio)))
    draw.ellipse((-180, -260, 520, 440), fill="#261052")
    draw.ellipse((860, 330, 1450, 920), fill="#1a0c3c")

    title_font = _font(58, True)
    subtitle_font = _font(28)
    stat_font = _font(44, True)
    label_font = _font(23)
    small_font = _font(20)

    draw.rounded_rectangle((54, 48, 1146, 672), radius=42, fill="#0c0918", outline="#7927ff", width=4)
    draw.rounded_rectangle((78, 72, 238, 232), radius=38, fill="#6f20ee", outline="#a654ff", width=3)
    draw.text((121, 101), "P", font=_font(88, True), fill="white")
    draw.polygon([(105, 158), (210, 118), (184, 189)], fill="#0a0714")

    draw.text((274, 78), "PHANTOM PROFILE", font=title_font, fill="white")
    draw.text((276, 151), data["name"], font=_font(34, True), fill="#b989ff")
    draw.text((276, 194), data["username"], font=subtitle_font, fill="#8f86a4")

    status_text = "● Telegram Business подключён" if data["connected"] else "● Telegram Business не подключён"
    status_color = "#54e69b" if data["connected"] else "#ff718c"
    draw.text((82, 265), status_text, font=_font(27, True), fill=status_color)

    cards = [
        ("💬", "Диалоги", data["dialogs"]),
        ("📦", "Сообщения", data["messages"]),
        ("✏", "Изменения", data["edited"]),
        ("🗑", "Удаления", data["deleted"]),
        ("🔐", "Скрытые медиа", data["protected"]),
    ]
    x_positions = [82, 300, 518, 736, 954]
    for x, (icon, label, value) in zip(x_positions, cards, strict=True):
        draw.rounded_rectangle((x, 334, x + 184, 562), radius=28, fill="#171025", outline="#3d275b", width=2)
        draw.text((x + 20, 357), icon, font=_font(35), fill="#b36cff")
        draw.text((x + 20, 421), str(value), font=stat_font, fill="white")
        draw.text((x + 20, 492), label, font=label_font, fill="#a99dbb")

    engagement = min(100, data["edited"] * 4 + data["deleted"] * 5 + data["protected"] * 8)
    draw.text((82, 596), "Активность архива", font=small_font, fill="#afa4c3")
    draw.rounded_rectangle((286, 600, 1040, 622), radius=11, fill="#241833")
    fill_width = int(754 * engagement / 100)
    if fill_width > 0:
        draw.rounded_rectangle((286, 600, 286 + fill_width, 622), radius=11, fill="#8b35ff")
    draw.text((1060, 592), f"{engagement}%", font=_font(23, True), fill="#d6b9ff")
    draw.text((82, 638), "Личная статистика обновляется при каждом открытии профиля", font=small_font, fill="#766d86")

    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


async def _send_profile(target: Message, telegram_id: int) -> None:
    data = await _stats(telegram_id)
    if data is None:
        await target.answer("Профиль ещё не создан. Отправьте /start.")
        return
    card = BufferedInputFile(_render_card(data), filename="phantom-profile.png")
    caption = (
        "<b>👤 Ваш профиль Phantom</b>\n\n"
        "Карточка собирается заново при каждом открытии и показывает актуальную статистику архива."
    )
    await target.answer_photo(card, caption=caption)


@router.message(Command("profile"))
async def profile_command(message: Message) -> None:
    if message.from_user:
        await _send_profile(message, message.from_user.id)


@router.callback_query(F.data == "user:profile")
async def profile_callback(callback: CallbackQuery) -> None:
    if callback.message:
        await _send_profile(callback.message, callback.from_user.id)
    await callback.answer()
