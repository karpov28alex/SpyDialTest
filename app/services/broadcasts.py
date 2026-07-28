from __future__ import annotations

from typing import Any

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, InputMediaVideo


def _markup(buttons: list[dict[str, str]]) -> InlineKeyboardMarkup | None:
    rows = [
        [InlineKeyboardButton(text=item["text"][:64], url=item["url"])]
        for item in buttons
        if item.get("text") and item.get("url")
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def send_broadcast(bot: Bot, payload: dict[str, Any]) -> None:
    telegram_id = int(payload["telegram_id"])
    text = str(payload.get("text") or "")
    media = list(payload.get("media") or [])[:10]
    markup = _markup(list(payload.get("buttons") or []))

    if not media:
        await bot.send_message(telegram_id, text or " ", reply_markup=markup)
        return

    if len(media) == 1:
        item = media[0]
        if item.get("type") == "photo":
            await bot.send_photo(
                telegram_id,
                photo=item["file_id"],
                caption=text or None,
                reply_markup=markup,
            )
        else:
            await bot.send_video(
                telegram_id,
                video=item["file_id"],
                caption=text or None,
                reply_markup=markup,
                supports_streaming=True,
            )
        return

    group = []
    for index, item in enumerate(media):
        caption = text if index == 0 and text else None
        if item.get("type") == "photo":
            group.append(InputMediaPhoto(media=item["file_id"], caption=caption))
        else:
            group.append(InputMediaVideo(media=item["file_id"], caption=caption))
    await bot.send_media_group(telegram_id, media=group)
    if markup:
        await bot.send_message(telegram_id, "Доступные действия:", reply_markup=markup)
