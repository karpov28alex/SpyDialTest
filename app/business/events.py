from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Iterable

from aiogram.types import Message as TgMessage

from app.db.models import Dialog, Media, Message, MessageVersion, UserSettings

MAX_NOTIFICATION_CONTENT = 1400


@dataclass(frozen=True, slots=True)
class ProtectedMediaDecision:
    allowed: bool
    reason: str


def safe_content(value: str | None) -> str:
    text = (value or "[медиа или пустое сообщение]").strip()
    if len(text) > MAX_NOTIFICATION_CONTENT:
        text = f"{text[:MAX_NOTIFICATION_CONTENT]}…"
    return html.escape(text)


def dialog_name(dialog: Dialog) -> str:
    return html.escape(str(dialog.peer_name or dialog.peer_username or dialog.telegram_chat_id))


def actor_name(dialog: Dialog) -> str:
    if dialog.peer_username:
        username = str(dialog.peer_username).lstrip("@")
        return f"@{html.escape(username)}"
    return dialog_name(dialog)


def _timestamp(value: datetime | None) -> str:
    return (value or datetime.now(UTC)).astimezone(UTC).strftime("%d.%m.%Y · %H:%M:%S")


def _previous_content(message: Message, versions: Iterable[MessageVersion]) -> str:
    ordered = sorted(versions, key=lambda item: item.version_number)
    if ordered:
        last = ordered[-1]
        return safe_content(last.text or last.caption)
    return safe_content(None)


def format_edit_notification(
    *,
    dialog: Dialog,
    settings: UserSettings,
    message: Message,
    versions: Iterable[MessageVersion],
) -> str:
    # Message history is the core feature of the service, therefore edit
    # notifications always contain both versions even when generic previews
    # are hidden in other notification types.
    return (
        f"❗️ <b>{actor_name(dialog)} изменил(а) сообщение</b>\n"
        f"🕓 <b>Отправлено:</b> {_timestamp(message.sent_at)}\n"
        f"✏️ <b>Изменено:</b> {_timestamp(message.edited_at)}\n\n"
        f"<b>Старое сообщение:</b>\n<blockquote>{_previous_content(message, versions)}</blockquote>\n\n"
        f"<b>Новое сообщение:</b>\n<blockquote>{safe_content(message.text or message.caption)}</blockquote>"
    )


def format_delete_notification(*, dialog: Dialog, settings: UserSettings, message: Message) -> str:
    # Deleted content must remain available in Telegram without requiring the
    # Mini App; this is independent from the generic preview preference.
    saved = f"<blockquote>{safe_content(message.text or message.caption)}</blockquote>"
    return (
        f"🗑 <b>{actor_name(dialog)} удалил сообщение</b>\n"
        f"🕓 <b>Отправлено:</b> {_timestamp(message.sent_at)}\n"
        f"🕓 <b>Удалено:</b> {_timestamp(message.deleted_at)}\n\n"
        f"<b>Сохранённое содержимое:</b>\n{saved}"
    )


def is_protected_message(event: TgMessage) -> ProtectedMediaDecision:
    if bool(event.has_protected_content):
        return ProtectedMediaDecision(True, "has_protected_content")
    raw: dict[str, Any] = event.model_dump(mode="python", exclude_none=True)
    for key in (
        "self_destruct_type",
        "ttl_seconds",
        "media_ttl_seconds",
        "is_view_once",
        "self_destruct_in",
    ):
        value = raw.get(key)
        if value not in (None, False, 0, ""):
            return ProtectedMediaDecision(True, key)
    return ProtectedMediaDecision(False, "no_explicit_protection_signal")


def protected_reply_is_allowed(*, media: Media, reply_message: Message) -> ProtectedMediaDecision:
    if media.is_protected is not True:
        return ProtectedMediaDecision(False, "stored_media_not_protected")
    if reply_message.reply_to_message_id is None:
        return ProtectedMediaDecision(False, "reply_target_missing")
    return ProtectedMediaDecision(True, "stored_protected_media_and_explicit_reply")
