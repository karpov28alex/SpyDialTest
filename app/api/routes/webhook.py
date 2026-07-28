import traceback
import uuid

import structlog
from aiogram.types import Update
from fastapi import APIRouter, Header, HTTPException, Request
from redis.asyncio import Redis
from sqlalchemy import select

from app.bot.handlers import router as command_router
from app.bot.setup import bot, dispatcher
from app.business.events import (
    format_delete_notification,
    format_edit_notification,
    protected_reply_is_allowed,
)
from app.core.config import get_settings
from app.db.models import Dialog, FailedUpdate, Media, Message, User, UserSettings
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
dispatcher.include_router(command_router)
redis = Redis.from_url(settings.redis_url, decode_responses=True)


async def _owner_context(
    session, message: Message
) -> tuple[User, Dialog, UserSettings] | None:
    dialog = await session.get(Dialog, message.dialog_id)
    if dialog is None:
        return None
    user = await session.get(User, dialog.owner_user_id)
    if user is None:
        return None
    prefs = user.settings or await session.get(UserSettings, user.id)
    if prefs is None:
        return None
    return user, dialog, prefs


async def _queue_connection_notification(session, *, update_id: int, connection) -> None:
    user = await session.get(User, connection.owner_user_id)
    if not user or not user.settings or not user.settings.notify_connection:
        return
    text = (
        "✅ <b>Telegram Business подключён</b>\n\n"
        "Dialog Spy начал сохранять поддерживаемые бизнес-диалоги."
        if connection.is_active
        else "⚠️ <b>Telegram Business отключён</b>\n\n"
        "Новые сообщения больше не сохраняются. Архив остаётся доступным."
    )
    await enqueue_job(
        session,
        redis,
        kind="send_text",
        payload={"telegram_id": user.telegram_id, "text": text},
        idempotency_key=f"connection:{update_id}:{int(connection.is_active)}",
    )


async def _queue_media_downloads(session, message: Message) -> list[Media]:
    rows = list(
        (await session.scalars(select(Media).where(Media.message_id == message.id))).all()
    )
    for media in rows:
        await enqueue_job(
            session,
            redis,
            kind="download_media",
            payload={"media_id": media.id},
            idempotency_key=f"media:{media.id}",
        )
    return rows


async def _queue_protected_reply(session, reply_message: Message) -> None:
    if reply_message.reply_to_message_id is None:
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
    context = await _owner_context(session, reply_message)
    if context is None or not context[2].notify_protected_media:
        return
    user, dialog, _ = context
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


@router.post("/telegram/webhook/{secret}", status_code=200)
async def telegram_webhook(
    secret: str,
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, bool]:
    if (
        secret != settings.telegram_webhook_secret
        and x_telegram_bot_api_secret_token != settings.telegram_webhook_secret
    ):
        raise HTTPException(status_code=403, detail="Forbidden")

    payload = await request.json()
    correlation_id = request.headers.get("x-correlation-id") or uuid.uuid4().hex
    kind = update_kind(payload)
    update_id = payload.get("update_id")
    if not isinstance(update_id, int):
        raise HTTPException(status_code=400, detail="Invalid update")

    logger.info(
        "telegram_update_received",
        update_id=update_id,
        kind=kind,
        correlation_id=correlation_id,
    )

    try:
        update = Update.model_validate(payload, context={"bot": bot})
        async with SessionLocal() as session, session.begin():
            if not await claim_update(session, update_id, kind):
                logger.info("telegram_update_duplicate", update_id=update_id, kind=kind)
                return {"ok": True}

            if update.business_connection:
                connection = await upsert_business_connection(session, update.business_connection)
                if connection:
                    await _queue_connection_notification(
                        session, update_id=update_id, connection=connection
                    )

            elif update.business_message:
                message, created = await save_business_message(session, update.business_message)
                if message and created:
                    await _queue_media_downloads(session, message)
                    await _queue_protected_reply(session, message)

            elif update.edited_business_message:
                message, changed, old_content = await edit_business_message(
                    session, update.edited_business_message
                )
                if message and changed:
                    context = await _owner_context(session, message)
                    if context and context[2].notify_edits:
                        user, dialog, prefs = context
                        text = format_edit_notification(
                            dialog=dialog,
                            settings=prefs,
                            old_content=old_content,
                            new_content=message.text or message.caption,
                            edited_at=message.edited_at,
                        )
                        await enqueue_job(
                            session,
                            redis,
                            kind="send_text",
                            payload={"telegram_id": user.telegram_id, "text": text},
                            idempotency_key=f"edit:{update_id}:{message.id}",
                        )

            elif update.deleted_business_messages:
                deleted = await delete_business_messages(
                    session, update.deleted_business_messages
                )
                for index, message in enumerate(deleted):
                    if message is None:
                        continue
                    context = await _owner_context(session, message)
                    if context and context[2].notify_deletions:
                        user, dialog, prefs = context
                        await enqueue_job(
                            session,
                            redis,
                            kind="send_text",
                            payload={
                                "telegram_id": user.telegram_id,
                                "text": format_delete_notification(
                                    dialog=dialog, settings=prefs, message=message
                                ),
                            },
                            idempotency_key=f"delete:{update_id}:{index}:{message.id}",
                        )

            else:
                await dispatcher.feed_update(bot, update)

            await finish_update(session, update_id)

        logger.info(
            "telegram_update_processed",
            update_id=update_id,
            kind=kind,
            correlation_id=correlation_id,
        )
        return {"ok": True}
    except Exception as exc:
        logger.exception(
            "telegram_update_failed",
            update_id=update_id,
            kind=kind,
            correlation_id=correlation_id,
        )
        async with SessionLocal() as session, session.begin():
            session.add(
                FailedUpdate(
                    update_id=update_id,
                    update_type=kind,
                    payload=payload,
                    error=str(exc),
                    stack_trace=traceback.format_exc(),
                    attempts=1,
                    resolved=False,
                    correlation_id=correlation_id,
                )
            )
        # Telegram receives 200 to avoid an uncontrolled retry storm; failed_updates
        # remains the durable retry source for administrators/workers.
        return {"ok": True}
