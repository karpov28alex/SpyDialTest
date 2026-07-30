from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from aiogram.types import BusinessConnection as TgBusinessConnection
from aiogram.types import BusinessMessagesDeleted, Message as TgMessage
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.business.events import is_protected_message
from app.db.models import BusinessConnection, Dialog, Media, Message, MessageVersion, ProcessedUpdate, User
from app.services.users import activate_business_trial, register_or_update_user


def update_kind(payload: dict[str, Any]) -> str:
    for key in ("business_connection", "business_message", "edited_business_message", "deleted_business_messages", "message", "callback_query"):
        if key in payload:
            return key
    return "unknown"


async def claim_update(session: AsyncSession, update_id: int, kind: str) -> bool:
    statement = insert(ProcessedUpdate).values(
        update_id=update_id,
        update_type=kind,
        processed_at=datetime.now(UTC),
        status="processing",
    ).on_conflict_do_nothing(index_elements=[ProcessedUpdate.update_id]).returning(ProcessedUpdate.update_id)
    return (await session.scalar(statement)) is not None


async def finish_update(session: AsyncSession, update_id: int) -> None:
    row = await session.get(ProcessedUpdate, update_id)
    if row:
        row.status = "processed"
        row.processed_at = datetime.now(UTC)


async def upsert_business_connection(session: AsyncSession, event: TgBusinessConnection) -> BusinessConnection | None:
    owner = await session.scalar(select(User).where(User.telegram_id == event.user.id).with_for_update())
    if owner is None:
        owner, _ = await register_or_update_user(
            session,
            telegram_id=event.user.id,
            username=event.user.username,
            first_name=event.user.first_name,
            last_name=event.user.last_name,
            language_code=event.user.language_code,
        )
    row = await session.scalar(
        select(BusinessConnection)
        .where(BusinessConnection.telegram_connection_id == event.id)
        .with_for_update()
    )
    now = datetime.now(UTC)
    rights = event.rights.model_dump(mode="json") if event.rights else {}
    if row is None:
        row = BusinessConnection(
            owner_user_id=owner.id,
            telegram_connection_id=event.id,
            business_user_id=event.user.id,
            is_active=event.is_enabled,
            rights=rights,
            connected_at=now,
            disconnected_at=None if event.is_enabled else now,
            last_activity_at=now,
        )
        session.add(row)
    else:
        row.owner_user_id = owner.id
        row.business_user_id = event.user.id
        row.is_active = event.is_enabled
        row.rights = rights
        row.last_activity_at = now
        row.disconnected_at = None if event.is_enabled else now
    if event.is_enabled:
        activate_business_trial(owner, now)
    return row


async def _connection_for_message(session: AsyncSession, connection_id: str) -> BusinessConnection | None:
    return await session.scalar(
        select(BusinessConnection).where(
            BusinessConnection.telegram_connection_id == connection_id,
            BusinessConnection.is_active.is_(True),
        )
    )


async def _dialog_for_message(session: AsyncSession, connection: BusinessConnection, event: TgMessage) -> Dialog:
    dialog = await session.scalar(
        select(Dialog).where(
            Dialog.business_connection_id == connection.id,
            Dialog.telegram_chat_id == event.chat.id,
        )
    )
    peer = event.chat
    peer_name = " ".join(part for part in [peer.first_name, peer.last_name] if part) or peer.title
    if dialog is None:
        dialog = Dialog(
            owner_user_id=connection.owner_user_id,
            business_connection_id=connection.id,
            telegram_chat_id=event.chat.id,
            peer_telegram_id=event.chat.id,
            peer_username=peer.username,
            peer_name=peer_name,
            last_message_at=event.date,
        )
        session.add(dialog)
        await session.flush()
    else:
        dialog.peer_username = peer.username
        dialog.peer_name = peer_name
        dialog.last_message_at = max(dialog.last_message_at or event.date, event.date)
    return dialog


def _media_from_message(event: TgMessage) -> list[dict[str, Any]]:
    decision = is_protected_message(event)
    result: list[dict[str, Any]] = []
    candidates = [
        ("photo", event.photo[-1] if event.photo else None),
        ("video", event.video),
        ("voice", event.voice),
        ("video_note", event.video_note),
        ("document", event.document),
        ("animation", event.animation),
        ("audio", event.audio),
        ("sticker", event.sticker),
    ]
    for media_type, item in candidates:
        if item is None:
            continue
        result.append({
            "media_type": media_type,
            "telegram_file_id": item.file_id,
            "telegram_unique_file_id": item.file_unique_id,
            "mime_type": getattr(item, "mime_type", None),
            "filename": getattr(item, "file_name", None),
            "size": getattr(item, "file_size", None),
            "duration": getattr(item, "duration", None),
            "width": getattr(item, "width", None),
            "height": getattr(item, "height", None),
            "is_protected": decision.allowed,
        })
    return result


async def save_business_message(session: AsyncSession, event: TgMessage) -> tuple[Message | None, bool]:
    if not event.business_connection_id:
        return None, False
    connection = await _connection_for_message(session, event.business_connection_id)
    if connection is None:
        return None, False
    dialog = await _dialog_for_message(session, connection, event)
    existing = await session.scalar(
        select(Message).where(
            Message.business_connection_id == connection.id,
            Message.telegram_chat_id == event.chat.id,
            Message.telegram_message_id == event.message_id,
        )
    )
    if existing:
        return existing, False
    direction = "outgoing" if event.from_user and event.from_user.id == connection.business_user_id else "incoming"
    message = Message(
        dialog_id=dialog.id,
        business_connection_id=connection.id,
        telegram_chat_id=event.chat.id,
        telegram_message_id=event.message_id,
        sender_id=event.from_user.id if event.from_user else None,
        direction=direction,
        text=event.text,
        caption=event.caption,
        reply_to_message_id=event.reply_to_message.message_id if event.reply_to_message else None,
        sent_at=event.date,
        raw_metadata=event.model_dump(mode="json", exclude_none=True),
    )
    session.add(message)
    await session.flush()
    session.add(MessageVersion(
        message_id=message.id,
        version_number=1,
        text=message.text,
        caption=message.caption,
        created_at=message.sent_at,
    ))
    for data in _media_from_message(event):
        session.add(Media(message_id=message.id, **data))
    connection.last_activity_at = datetime.now(UTC)
    return message, True


async def edit_business_message(session: AsyncSession, event: TgMessage) -> tuple[Message | None, bool, str | None]:
    if not event.business_connection_id:
        return None, False, None
    connection = await _connection_for_message(session, event.business_connection_id)
    if connection is None:
        return None, False, None
    message = await session.scalar(
        select(Message).where(
            Message.business_connection_id == connection.id,
            Message.telegram_chat_id == event.chat.id,
            Message.telegram_message_id == event.message_id,
        ).with_for_update()
    )
    if message is None:
        message, _ = await save_business_message(session, event)
        return message, False, None
    if message.text == event.text and message.caption == event.caption:
        return message, False, None
    old_content = message.text or message.caption
    current_version = int(
        await session.scalar(
            select(func.coalesce(func.max(MessageVersion.version_number), 0))
            .where(MessageVersion.message_id == message.id)
        ) or 0
    )
    latest = await session.scalar(
        select(MessageVersion)
        .where(MessageVersion.message_id == message.id)
        .order_by(MessageVersion.version_number.desc())
        .limit(1)
    )
    if latest is None or latest.text != message.text or latest.caption != message.caption:
        session.add(MessageVersion(
            message_id=message.id,
            version_number=current_version + 1,
            text=message.text,
            caption=message.caption,
            created_at=message.edited_at or datetime.now(UTC),
        ))
    message.text = event.text
    message.caption = event.caption
    message.edited_at = event.edit_date or datetime.now(UTC)
    message.raw_metadata = event.model_dump(mode="json", exclude_none=True)
    connection.last_activity_at = datetime.now(UTC)
    return message, True, old_content


async def delete_business_messages(session: AsyncSession, event: BusinessMessagesDeleted) -> list[Message | None]:
    connection = await _connection_for_message(session, event.business_connection_id)
    if connection is None:
        return []
    now = datetime.now(UTC)
    results: list[Message | None] = []
    for telegram_message_id in event.message_ids:
        message = await session.scalar(
            select(Message).where(
                Message.business_connection_id == connection.id,
                Message.telegram_chat_id == event.chat.id,
                Message.telegram_message_id == telegram_message_id,
            ).with_for_update()
        )
        if message is None or message.is_deleted:
            results.append(None)
            continue
        message.is_deleted = True
        message.deleted_at = now
        results.append(message)
    connection.last_activity_at = now
    return results
