from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from io import BytesIO
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from app.bot.enhanced_user_menu import enhanced_user_keyboard
from app.bot.statistics_card_handlers import (
    _circle_avatar,
    _collect_stats,
    _duration_text,
    _format_number,
    _leader_avatars,
    _load_logo,
)
from app.db.models import User

router = Router(name="statistics-card-v2")
logger = logging.getLogger(__name__)


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = (
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    )
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    raise RuntimeError("Cyrillic font is not installed")


def _plain(value: str, limit: int = 92) -> str:
    # PNG uses a normal Cyrillic font. Emoji and variation selectors are removed
    # deliberately because monochrome server fonts render them as square boxes.
    value = value.replace("\ufe0f", "").replace("\u200d", "")
    value = re.sub(r"[^0-9A-Za-zА-Яа-яЁё@._\-—·,:() №]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()[:limit]


def _glass(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], radius: int = 26) -> None:
    draw.rounded_rectangle(box, radius=radius, fill="#120b22", outline="#7130a8", width=2)


def _render(stats: dict[str, Any], avatars: dict[str, bytes | None]) -> bytes:
    image = Image.new("RGB", (1080, 1080), "#05030d")
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
    owner_name = _plain(" ".join(x for x in (owner.first_name, owner.last_name) if x) or "Пользователь", 34)
    username = f"@{owner.username}" if owner.username else f"ID {owner.telegram_id}"
    draw.text((224, 149), f"{owner_name}  ·  {username}", font=_font(19), fill="#cbbbd8")
    now = datetime.now(UTC)
    draw.rounded_rectangle((820, 58, 1019, 151), radius=25, fill="#130b23", outline="#4b226d", width=2)
    draw.text((850, 77), now.strftime("Сегодня · %H:%M"), font=_font(20, True), fill="#ffffff")
    status = "TELEGRAM BUSINESS ПОДКЛЮЧЁН" if stats["connected"] else "TELEGRAM BUSINESS НЕ ПОДКЛЮЧЁН"
    color = "#4ee29a" if stats["connected"] else "#ff6b86"
    draw.rounded_rectangle((222, 182, 615, 218), radius=18, fill="#120b22", outline="#43205f", width=2)
    draw.ellipse((238, 193, 252, 207), fill=color)
    draw.text((266, 188), status, font=_font(16, True), fill=color)

    totals = stats["totals"]
    metrics = [
        ("ДИАЛОГИ", totals["dialogs"]), ("СООБЩЕНИЯ", totals["messages"]),
        ("ИЗМЕНЕНИЯ", totals["edited"]), ("УДАЛЕНИЯ", totals["deleted"]),
        ("СКРЫТЫЕ МЕДИА", totals["protected"]),
    ]
    _glass(draw, (54, 245, 1026, 408), 30)
    for index, (label, value) in enumerate(metrics):
        x = 76 + index * 190
        if index:
            draw.line((x - 20, 278, x - 20, 376), fill="#3d2455", width=2)
        draw.text((x, 276), _format_number(value), font=_font(35, True), fill="#ffffff")
        draw.text((x, 338), label, font=_font(14, True), fill="#9e91aa")

    leader_specs = [
        ("active", "БОЛЬШЕ ВСЕГО ОБЩЕНИЯ", "сообщений"),
        ("media", "БОЛЬШЕ ВСЕГО МЕДИА", "медиа"),
        ("protected", "БОЛЬШЕ СКРЫТЫХ МЕДИА", "файлов"),
        ("deleted", "БОЛЬШЕ ВСЕГО УДАЛЕНИЙ", "удалений"),
    ]
    for index, (key, title, unit) in enumerate(leader_specs):
        col, row = index % 2, index // 2
        x1, y1 = 54 + col * 492, 438 + row * 202
        _glass(draw, (x1, y1, x1 + 466, y1 + 180))
        draw.text((x1 + 22, y1 + 18), title, font=_font(15, True), fill="#b266ff")
        item = stats["leaders"].get(key)
        if not item:
            draw.text((x1 + 22, y1 + 78), "Недостаточно данных", font=_font(22, True), fill="#ffffff")
            continue
        _circle_avatar(image, avatars.get(key), (x1 + 82, y1 + 108), 88, item["name"])
        draw.text((x1 + 145, y1 + 63), _plain(item["name"], 24), font=_font(23, True), fill="#ffffff")
        draw.text((x1 + 145, y1 + 96), _plain(item["username"] or "Без username", 26), font=_font(17), fill="#a99bb5")
        draw.text((x1 + 145, y1 + 128), f"{_format_number(item['value'])} {unit}", font=_font(19, True), fill="#c270ff")

    _glass(draw, (54, 854, 1026, 1015), 28)
    draw.text((78, 875), "ИНТЕРЕСНЫЕ ФАКТЫ", font=_font(19, True), fill="#b464ff")
    for index, text in enumerate(stats["insights"][:4]):
        draw.text((78, 912 + index * 25), _plain(text), font=_font(15), fill="#d9d0df")
    longest = stats["leaders"].get("longest")
    fastest = stats["leaders"].get("fastest")
    if longest:
        draw.text((610, 875), f"Самый долгий диалог: {longest['days']} дн.", font=_font(16, True), fill="#ffffff")
        draw.text((610, 903), _plain(f"{longest['name']} · {_format_number(longest['value'])} сообщений", 48), font=_font(14), fill="#a99bb5")
    if fastest:
        draw.text((610, 941), f"Самый быстрый ответ: {_duration_text(fastest['seconds'])}", font=_font(16, True), fill="#ffffff")
        draw.text((610, 969), _plain(fastest["name"], 32), font=_font(14), fill="#a99bb5")
    draw.text((760, 1026), "Статистика сформирована Phantom Spy", font=_font(13), fill="#766b82")

    output = BytesIO()
    image.save(output, "PNG", optimize=True)
    return output.getvalue()


async def _send(target: Message, telegram_id: int) -> None:
    await target.answer("Собираю актуальную статистику и вычисляю лидеров…")
    stats = await _collect_stats(telegram_id)
    if stats is None:
        await target.answer("Профиль ещё не создан. Отправьте /start.", reply_markup=enhanced_user_keyboard())
        return
    try:
        avatars = await _leader_avatars(stats)
        card = BufferedInputFile(_render(stats, avatars), filename="phantom-statistics.png")
        await target.answer_photo(card, caption="<b>Персональная статистика Phantom</b>\n\nДанные обновлены автоматически.", reply_markup=enhanced_user_keyboard())
    except Exception:
        logger.exception("statistics_card_v2_failed", extra={"telegram_id": telegram_id})
        totals = stats["totals"]
        await target.answer(
            "<b>Персональная статистика Phantom</b>\n\n"
            f"Диалогов: <b>{totals['dialogs']}</b>\nСообщений: <b>{totals['messages']}</b>\n"
            f"Изменений: <b>{totals['edited']}</b>\nУдалений: <b>{totals['deleted']}</b>\n"
            f"Скрытых медиа: <b>{totals['protected']}</b>",
            reply_markup=enhanced_user_keyboard(),
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
