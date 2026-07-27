import html
import traceback
import uuid
from datetime import UTC, datetime

import structlog
from aiogram.types import Update
from fastapi import APIRouter, Header, HTTPException, Request
from redis.asyncio import Redis
from sqlalchemy import select

from app.bot.handlers import router as command_router
from app.bot.setup import bot, dispatcher
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


def _name(dialog: Dialog) -> str:
    return html.escape(str(dialog.peer_name or dialog.peer_username or dialog.telegram_chat_id))


def _content(value: str | None) -> str:
    return html.escape(value or "[медиа или пустое сообщение]")


def _icon(prefs: UserSettings, value: str) -> str:
    return value if getattr(prefs, "notify_emoji", True) else ""


async def _owner_context(session, message: Message) -> tuple[User, Dialog, UserSettings] | None:
    dialog = await session.get(Dialog, message.dialog_id)
    if not dialog:
        return None
    user = await session.get(User, dialog.owner_user_id)
    if not user:
        return None
    prefs = user.settings or await session.get(UserSettings, user.id)
    if not prefs:
        return None
    return user, dialog, prefs


@router.post("/telegram/webhook/{secret}", status_code=200)
async def telegram_webhook(
    secret: str,
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, bool]:
    if secret != settings.telegram_webhook_secret and x_telegram_bot_api_secret_token != settings.telegram_webhook_secret:
        raise HTTPException(status_code=403, detail="Forbidden")

    payload = await request.json()
    correlation_id = request.headers.get("x-correlation-id") or uuid.uuid4().hex
    kind = update_kind(payload)
    update_id = payload.get("update_id")
    if not isinstance(update_id, int):
        raise HTTPException(status_code=400, detail="Invalid update")

    try:
        update = Update.model_validate(payload, context={"bot": bot})
        async with SessionLocal() as session, session.begin():
            if not await claim_update(session, update_id, kind):
                return {"ok": True}

            if update.business_connection:
                connection = await upsert_business_connection(session, update.business_connection)
                if connection:
                    user = await session.get(User, connection.owner_user_id)
                    if user and user.settings and user.settings.notify_connection:
                        text = (
                            "✅ <b>Telegram Business подключён</b>\n\nDialog Spy начал сохранять поддерживаемые бизнес-диалоги."
                            if connection.is_active
                            else "⚠️ <b>Telegram Business отключён</b>\n\nНовые сообщения больше не сохраняются. Архив остаётся доступным."
                        )
                        await enqueue_job(session, redis, kind="send_text", payload={"telegram_id": user.telegram_id, "text": text}, idempotency_key=f"connection:{update_id}")

            elif update.business_message:
                message, created = await save_business_message(session, update.business_message)
                if message and created:
                    media_rows = list((await session.scalars(select(Media).where(Media.message_id == message.id))).all())
                    for media in media_rows:
                        await enqueue_job(session, redis, kind="download_media", payload={"media_id": media.id}, idempotency_key=f"media:{media.id}")

                    # Важно: опираемся на уже сохранённый оригинал, а не на неполный объект reply Telegram.
                    if message.reply_to_message_id:
                        protected = await session.scalar(
                            select(Media)
                            .join(Message)
                            .where(
                                Message.business_connection_id == message.business_connection_id,
                                Message.telegram_chat_id == message.telegram_chat_id,
                                Message.telegram_message_id == message.reply_to_message_id,
                                Media.is_protected.is_(True),
                            )
                            .order_by(Media.id)
                        )
                        if protected:
                            context = await _owner_context(session, message)
                            if context and context[2].notify_protected_media:
                                await enqueue_job(
                                    session,
                                    redis,
                                    kind="deliver_protected_media",
                                    payload={"media_id": protected.id, "owner_user_id": context[0].id, "dialog_name": context[1].peer_name or context[1].peer_username},
                                    idempotency_key=f"protected-reply:{message.id}:{protected.id}",
                                )

            elif update.edited_business_message:
                message, changed, old_content = await edit_business_message(session, update.edited_business_message)
                if message and changed:
                    context = await _owner_context(session, message)
                    if context and context[2].notify_edits:
                        user, dialog, prefs = context
                        title = f"{_icon(prefs, '✏️ ')}<b>Сообщение изменено</b>"
                        if prefs.hide_preview:
                            text = f"{title}\n\n<b>Диалог:</b> {_name(dialog)}\n\nОткройте Dialog Spy, чтобы посмотреть оригинал и новую версию."
                        else:
                            text = (
                                f"{title}\n\n<b>Диалог:</b> {_name(dialog)}\n"
                                f"<b>Время:</b> {message.edited_at.astimezone(UTC).strftime('%d.%m.%Y · %H:%M UTC') if message.edited_at else '—'}\n\n"
                                f"<b>Было:</b>\n<blockquote>{_content(old_content)}</blockquote>\n\n"
                                f"<b>Стало:</b>\n<blockquote>{_content(message.text or message.caption)}</blockquote>"
                            )
                        await enqueue_job(session, redis, kind="send_text", payload={"telegram_id": user.telegram_id, "text": text}, idempotency_key=f"edit:{update_id}:{message.id}")

            elif update.deleted_business_messages:
                deleted = await delete_business_messages(session, update.deleted_business_messages)
                for index, message in enumerate(deleted):
                    if message is None:
                        continue
                    context = await _owner_context(session, message)
                    if context and context[2].notify_deletions:
                        user, dialog, prefs = context
                        title = f"{_icon(prefs, '🗑 ')}<b>Сообщение удалено</b>"
                        if prefs.hide_preview:
                            saved = "Откройте Dialog Spy, чтобы посмотреть сохранённую копию."
                        else:
                            saved = f"<blockquote>{_content(message.text or message.caption)}</blockquote>"
                        text = (
                            f"{title}\n\n<b>Диалог:</b> {_name(dialog)}\n"
                            f"<b>Отправлено:</b> {message.sent_at.astimezone(UTC).strftime('%d.%m.%Y · %H:%M UTC')}\n"
                            f"<b>Удалено:</b> {message.deleted_at.astimezone(UTC).strftime('%d.%m.%Y · %H:%M UTC') if message.deleted_at else '—'}\n\n"
                            f"<b>Сохранённое содержимое:</b>\n{saved}"
                        )
                        await enqueue_job(session, redis, kind="send_text", payload={"telegram_id": user.telegram_id, "text": text}, idempotency_key=f"delete:{update_id}:{index}:{message.id}")
            else:
                await dispatcher.feed_update(bot, update)

            await finish_update(session, update_id)
        return {"ok": True}
    except Exception as exc:
        logger.exception("telegram_update_failed", update_id=update_id, kind=kind, correlation_id=correlation_id)
        async with SessionLocal() as session, session.begin():
            session.add(FailedUpdate(update_id=update_id, update_type=kind, payload=payload, error=str(exc), stack_trace=traceback.format_exc(), attempts=1, resolved=False, correlation_id=correlation_id))
        return {"ok": True}
