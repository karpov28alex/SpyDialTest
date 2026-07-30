import traceback
import uuid

import structlog
from aiogram.types import Update
from fastapi import APIRouter, Header, HTTPException, Request
from redis.asyncio import Redis
from sqlalchemy import select

from app.bot.admin_handlers import router as admin_router
from app.bot.handlers import router as command_router
from app.bot.setup import bot, dispatcher
from app.business.events import format_delete_notification, format_edit_notification, protected_reply_is_allowed
from app.core.config import get_settings
from app.db.models import BusinessConnection, Dialog, FailedUpdate, Media, Message, MessageVersion, User, UserSettings
from app.db.session import SessionLocal
from app.services.queue import enqueue_job
from app.services.telegram_updates import (
    claim_update,
    delete_business_messages,
    edit_business_message,
    finish_update,
    save_business_message,
    update_kind,
    upsert_business_connection,
)

router = APIRouter(tags=["telegram"])
settings = get_settings()
logger = structlog.get_logger()
dispatcher.include_router(admin_router)
dispatcher.include_router(command_router)
redis = Redis.from_url(settings.redis_url, decode_responses=True)


async def _ensure_preferences(session, user: User) -> UserSettings:
    prefs = user.settings or await session.get(UserSettings, user.id)
    if prefs is None:
        prefs = UserSettings(user_id=user.id, language=user.language_code or "ru")
        session.add(prefs)
        await session.flush()
    return prefs


async def _owner_context(session, message: Message) -> tuple[User, Dialog, UserSettings] | None:
    dialog = await session.get(Dialog, message.dialog_id)
    if dialog is None:
        return None
    user = await session.get(User, dialog.owner_user_id)
    if user is None:
        return None
    return user, dialog, await _ensure_preferences(session, user)


async def _preferences_for_connection(session, connection_id: str) -> UserSettings | None:
    connection = await session.scalar(
        select(BusinessConnection).where(BusinessConnection.telegram_connection_id == connection_id)
    )
    if connection is None:
        return None
    user = await session.get(User, connection.owner_user_id)
    if user is None:
        return None
    return await _ensure_preferences(session, user)


async def _queue_connection_notification(session, *, update_id: int, connection: BusinessConnection) -> None:
    user = await session.get(User, connection.owner_user_id)
    if not user:
        return
    prefs = await _ensure_preferences(session, user)
    if not prefs.notifications_enabled or not prefs.notify_connection:
        return
    text = (
        "✅ <b>Telegram Business подключён</b>\n\nDialog Spy начал сохранять поддерживаемые бизнес-диалоги."
        if connection.is_active
        else "⚠️ <b>Telegram Business отключён</b>\n\nНовые сообщения больше не сохраняются. Архив остаётся доступным."
    )
    await enqueue_job(
        session,
        redis,
        kind="send_text",
        payload={"telegram_id": user.telegram_id, "text": text},
        idempotency_key=f"connection:{update_id}:{int(connection.is_active)}",
    )


async def _queue_media_downloads(session, message: Message) -> list[Media]:
    rows = list((await session.scalars(select(Media).where(Media.message_id == message.id))).all())
    for media in rows:
        if media.download_status == "downloaded":
            continue
        await enqueue_job(
            session,
            redis,
            kind="download_media",
            payload={"media_id": media.id},
            idempotency_key=f"media:{media.id}",
        )
    return rows


async def _persist_embedded_reply(session, event) -> None:
    """Capture an embedded target only when Telegram never sent its original message.

    Ordinary media messages are already present in the archive. Promoting an existing
    message merely because it appears inside reply_to_message caused false protected
    media notifications. A missing original is the reliable fallback used for
    one-time/protected media that Telegram exposes only inside the explicit reply.
    """
    target = event.reply_to_message
    if target is None or not event.business_connection_id:
        return
    has_media = any((
        target.photo,
        target.video,
        target.voice,
        target.video_note,
        target.document,
        target.animation,
        target.audio,
        target.sticker,
    ))
    if not has_media:
        return
    prefs = await _preferences_for_connection(session, event.business_connection_id)
    if prefs is None or not prefs.save_protected_media:
        return
    connection = await session.scalar(
        select(BusinessConnection).where(
            BusinessConnection.telegram_connection_id == event.business_connection_id
        )
    )
    if connection is None:
        return
    existing = await session.scalar(
        select(Message).where(
            Message.business_connection_id == connection.id,
            Message.telegram_chat_id == target.chat.id,
            Message.telegram_message_id == target.message_id,
        )
    )
    if existing is not None:
        logger.info(
            "embedded_reply_target_already_archived",
            message_id=existing.id,
            telegram_message_id=target.message_id,
        )
        return
    target_with_connection = target.model_copy(update={"business_connection_id": event.business_connection_id})
    stored, created = await save_business_message(session, target_with_connection)
    if not stored or not created:
        return
    metadata = dict(stored.raw_metadata or {})
    metadata["_capture_reason"] = "embedded_reply_missing_original"
    stored.raw_metadata = metadata
    media_rows = list((await session.scalars(select(Media).where(Media.message_id == stored.id))).all())
    for media in media_rows:
        media.is_protected = True
    await session.flush()
    await _queue_media_downloads(session, stored)
    logger.info(
        "embedded_protected_reply_archived",
        message_id=stored.id,
        telegram_message_id=stored.telegram_message_id,
        media_count=len(media_rows),
        reason="embedded_reply_missing_original",
    )


async def _queue_protected_reply(session, reply_message: Message) -> None:
    if reply_message.reply_to_message_id is None:
        return
    context = await _owner_context(session, reply_message)
    if context is None:
        return
    user, dialog, prefs = context
    if not prefs.save_protected_media:
        return
    protected = await session.scalar(
        select(Media)
        .join(Message, Message.id == Media.message_id)
        .where(
            Message.business_connection_id == reply_message.business_connection_id,
            Message.telegram_chat_id == reply_message.telegram_chat_id,
            Message.telegram_message_id == reply_message.reply_to_message_id,
            Media.is_protected.is_(True),
        )
        .order_by(Media.id)
        .limit(1)
    )
    if protected is None:
        logger.info(
            "protected_reply_target_not_found",
            reply_message_id=reply_message.id,
            target_telegram_message_id=reply_message.reply_to_message_id,
        )
        return
    decision = protected_reply_is_allowed(media=protected, reply_message=reply_message)
    if not decision.allowed:
        logger.warning(
            "protected_media_delivery_blocked",
            media_id=protected.id,
            message_id=reply_message.id,
            reason=decision.reason,
        )
        return
    if not prefs.notifications_enabled or not prefs.notify_protected_media:
        return
    await enqueue_job(
        session,
        redis,
        kind="deliver_protected_media",
        payload={
            "media_id": protected.id,
            "owner_user_id": user.id,
            "dialog_name": dialog.peer_name or dialog.peer_username,
        },
        idempotency_key=f"protected-reply:{reply_message.id}:{protected.id}",
    )


async def _handle_webhook(
    secret: str,
    request: Request,
    x_telegram_bot_api_secret_token: str | None,
) -> dict[str, bool]:
    if secret != settings.telegram_webhook_secret and x_telegram_bot_api_secret_token != settings.telegram_webhook_secret:
        raise HTTPException(status_code=403, detail="Forbidden")

    payload = await request.json()
    correlation_id = request.headers.get("x-correlation-id") or uuid.uuid4().hex
    kind = update_kind(payload)
    update_id = payload.get("update_id")
    if not isinstance(update_id, int):
        raise HTTPException(status_code=400, detail="Invalid update")

    logger.info("telegram_update_received", update_id=update_id, kind=kind, correlation_id=correlation_id)

    try:
        update = Update.model_validate(payload, context={"bot": bot})
        async with SessionLocal() as session, session.begin():
            if not await claim_update(session, update_id, kind):
                logger.info("telegram_update_duplicate", update_id=update_id, kind=kind)
                return {"ok": True}

            if update.business_connection:
                connection = await upsert_business_connection(session, update.business_connection)
                if connection:
                    await _queue_connection_notification(session, update_id=update_id, connection=connection)

            elif update.business_message:
                await _persist_embedded_reply(session, update.business_message)
                message, created = await save_business_message(session, update.business_message)
                if message and created:
                    await _queue_media_downloads(session, message)
                    await _queue_protected_reply(session, message)

            elif update.edited_business_message:
                message, changed, _ = await edit_business_message(session, update.edited_business_message)
                if message and changed:
                    context = await _owner_context(session, message)
                    if context and context[2].notifications_enabled and context[2].notify_edits:
                        user, dialog, prefs = context
                        versions = list((await session.scalars(
                            select(MessageVersion)
                            .where(MessageVersion.message_id == message.id)
                            .order_by(MessageVersion.version_number)
                        )).all())
                        await enqueue_job(
                            session,
                            redis,
                            kind="send_text",
                            payload={
                                "telegram_id": user.telegram_id,
                                "text": format_edit_notification(
                                    dialog=dialog,
                                    settings=prefs,
                                    message=message,
                                    versions=versions,
                                ),
                            },
                            idempotency_key=f"edit:{update_id}:{message.id}",
                        )

            elif update.deleted_business_messages:
                deleted = await delete_business_messages(session, update.deleted_business_messages)
                for index, message in enumerate(deleted):
                    if message is None:
                        continue
                    context = await _owner_context(session, message)
                    if context and context[2].notifications_enabled and context[2].notify_deletions:
                        user, dialog, prefs = context
                        await enqueue_job(
                            session,
                            redis,
                            kind="send_text",
                            payload={
                                "telegram_id": user.telegram_id,
                                "text": format_delete_notification(dialog=dialog, settings=prefs, message=message),
                            },
                            idempotency_key=f"delete:{update_id}:{index}:{message.id}",
                        )
            else:
                await dispatcher.feed_update(bot, update)

            await finish_update(session, update_id)

        logger.info("telegram_update_processed", update_id=update_id, kind=kind, correlation_id=correlation_id)
        return {"ok": True}
    except Exception as exc:
        logger.exception("telegram_update_failed", update_id=update_id, kind=kind, correlation_id=correlation_id)
        async with SessionLocal() as session, session.begin():
            session.add(FailedUpdate(
                update_id=update_id,
                update_type=kind,
                payload=payload,
                error=str(exc),
                stack_trace=traceback.format_exc(),
                attempts=1,
                resolved=False,
                correlation_id=correlation_id,
            ))
        return {"ok": True}


@router.post("/telegram/webhook/{secret}", status_code=200)
async def telegram_webhook(
    secret: str,
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, bool]:
    return await _handle_webhook(secret, request, x_telegram_bot_api_secret_token)


@router.post("/api/telegram/webhook/{secret}", status_code=200, include_in_schema=False)
async def legacy_telegram_webhook(
    secret: str,
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, bool]:
    return await _handle_webhook(secret, request, x_telegram_bot_api_secret_token)
