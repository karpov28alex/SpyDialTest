from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from aiogram.types import Message as TgMessage

from app.db.models import Dialog, Media, Message, UserSettings

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


def _icon(settings: UserSettings, value: str) -> str:
    return value if settings.notify_emoji else ""


def format_edit_notification(
    *,
    dialog: Dialog,
    settings: UserSettings,
    old_content: str | None,
    new_content: str | None,
    edited_at: datetime | None,
) -> str:
    title = f"{_icon(settings, '✏️ ')}<b>Сообщение изменено</b>"
    if settings.hide_preview:
        return (
            f"{title}\n\n<b>Диалог:</b> {dialog_name(dialog)}\n\n"
            "Откройте Dialog Spy, чтобы посмотреть оригинал и новую версию."
        )
    timestamp = (edited_at or datetime.now(UTC)).astimezone(UTC).strftime("%d.%m.%Y · %H:%M UTC")
    return (
        f"{title}\n\n<b>Диалог:</b> {dialog_name(dialog)}\n"
        f"<b>Время:</b> {timestamp}\n\n"
        f"<b>Было:</b>\n<blockquote>{safe_content(old_content)}</blockquote>\n\n"
        f"<b>Стало:</b>\n<blockquote>{safe_content(new_content)}</blockquote>"
    )


def format_delete_notification(*, dialog: Dialog, settings: UserSettings, message: Message) -> str:
    title = f"{_icon(settings, '🗑 ')}<b>Сообщение удалено</b>"
    saved = (
        "Откройте Dialog Spy, чтобы посмотреть сохранённую копию."
        if settings.hide_preview
        else f"<blockquote>{safe_content(message.text or message.caption)}</blockquote>"
    )
    sent_at = message.sent_at.astimezone(UTC).strftime("%d.%m.%Y · %H:%M UTC")
    deleted_at = (
        message.deleted_at.astimezone(UTC).strftime("%d.%m.%Y · %H:%M UTC")
        if message.deleted_at
        else "—"
    )
    return (
        f"{title}\n\n<b>Диалог:</b> {dialog_name(dialog)}\n"
        f"<b>Отправлено:</b> {sent_at}\n"
        f"<b>Удалено:</b> {deleted_at}\n\n"
        f"<b>Сохранённое содержимое:</b>\n{saved}"
    )


def is_protected_message(event: TgMessage) -> ProtectedMediaDecision:
    """Classify only explicit Telegram protection/expiry signals.

    A reply alone is never enough. Unknown future Bot API fields are inspected in
    the raw model dump so support can be extended without weakening the invariant.
    """
    if bool(event.has_protected_content):
        return ProtectedMediaDecision(True, "has_protected_content")

    raw: dict[str, Any] = event.model_dump(mode="python", exclude_none=True)
    for key in ("self_destruct_type", "ttl_seconds", "media_ttl_seconds", "is_view_once"):
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
