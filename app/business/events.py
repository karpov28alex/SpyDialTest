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


def _icon(settings: UserSettings, value: str) -> str:
    return value if settings.notify_emoji else ""


def _timestamp(value: datetime | None) -> str:
    return (value or datetime.now(UTC)).astimezone(UTC).strftime("%d.%m.%Y · %H:%M:%S")


def format_edit_notification(
    *,
    dialog: Dialog,
    settings: UserSettings,
    message: Message,
    versions: Iterable[MessageVersion],
) -> str:
    title = f"{_icon(settings, '✏️ ')}<b>СООБЩЕНИЕ ИЗМЕНЕНО</b>"
    if settings.hide_preview:
        return (
            f"{title}\n━━━━━━━━━━━━━━━━━━\n"
            f"👤 {dialog_name(dialog)}\n💬 <b>Диалог:</b> {dialog_name(dialog)}\n"
            f"🕓 {_timestamp(message.edited_at)}\n\n"
            "Откройте Dialog Spy, чтобы посмотреть все версии."
        )

    parts = [
        title,
        "━━━━━━━━━━━━━━━━━━",
        f"👤 {dialog_name(dialog)}",
        f"💬 <b>Диалог:</b> {dialog_name(dialog)}",
        f"🕓 {_timestamp(message.edited_at)}",
        "",
    ]
    ordered = sorted(versions, key=lambda item: item.version_number)
    for version in ordered:
        parts.extend(
            [
                f"<b>Версия {version.version_number}</b>",
                f"🕓 {_timestamp(version.created_at)}",
                safe_content(version.text or version.caption),
                "",
            ]
        )
    parts.extend(
        [
            "<b>Текущая версия</b>",
            f"🕓 {_timestamp(message.edited_at)}",
            safe_content(message.text or message.caption),
            "━━━━━━━━━━━━━━━━━━",
            "Все версии сохранены в истории диалога.",
        ]
    )
    return "\n".join(parts)


def format_delete_notification(*, dialog: Dialog, settings: UserSettings, message: Message) -> str:
    title = f"{_icon(settings, '🗑 ')}<b>Сообщение удалено</b>"
    saved = (
        "Откройте Dialog Spy, чтобы посмотреть сохранённую копию."
        if settings.hide_preview
        else f"<blockquote>{safe_content(message.text or message.caption)}</blockquote>"
    )
    return (
        f"{title}\n\n<b>Диалог:</b> {dialog_name(dialog)}\n"
        f"<b>Отправлено:</b> {_timestamp(message.sent_at)}\n"
        f"<b>Удалено:</b> {_timestamp(message.deleted_at)}\n\n"
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
