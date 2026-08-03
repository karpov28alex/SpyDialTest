from __future__ import annotations

from collections import Counter

from fastapi import APIRouter
from sqlalchemy import func, select

from app.api.routes.admin import AdminAuth, Session
from app.bot.setup import bot
from app.db.models import BusinessConnection, Dialog, FailedUpdate, Media, Message, ProcessedUpdate

router = APIRouter(prefix="/api/admin/telegram", tags=["admin-telegram"])


@router.get("/coverage")
async def telegram_coverage(_: AdminAuth, session: Session) -> dict:
    webhook = await bot.get_webhook_info()

    processed_rows = list((await session.execute(
        select(ProcessedUpdate.update_type, func.count(ProcessedUpdate.update_id))
        .group_by(ProcessedUpdate.update_type)
    )).all())
    processed = {str(kind): int(count or 0) for kind, count in processed_rows}

    failed_rows = list((await session.execute(
        select(FailedUpdate.update_type, func.count(FailedUpdate.id))
        .where(FailedUpdate.resolved.is_(False))
        .group_by(FailedUpdate.update_type)
    )).all())
    failed = {str(kind): int(count or 0) for kind, count in failed_rows}

    connections = list((await session.scalars(
        select(BusinessConnection).order_by(BusinessConnection.last_activity_at.desc(), BusinessConnection.id.desc())
    )).all())
    rights_counter: Counter[str] = Counter()
    connection_items = []
    for connection in connections:
        rights = dict(connection.rights or {})
        for key, value in rights.items():
            if value is True:
                rights_counter[key] += 1
        connection_items.append({
            "id": connection.id,
            "owner_user_id": connection.owner_user_id,
            "telegram_connection_id": connection.telegram_connection_id,
            "business_user_id": connection.business_user_id,
            "active": connection.is_active,
            "rights": rights,
            "connected_at": connection.connected_at.isoformat() if connection.connected_at else None,
            "disconnected_at": connection.disconnected_at.isoformat() if connection.disconnected_at else None,
            "last_activity_at": connection.last_activity_at.isoformat() if connection.last_activity_at else None,
        })

    media_rows = list((await session.execute(
        select(Media.download_status, func.count(Media.id)).group_by(Media.download_status)
    )).all())
    media_statuses = {str(status or "unknown"): int(count or 0) for status, count in media_rows}

    return {
        "webhook": {
            "url": webhook.url,
            "pending_update_count": webhook.pending_update_count,
            "allowed_updates": list(webhook.allowed_updates or []),
            "last_error_date": webhook.last_error_date.isoformat() if webhook.last_error_date else None,
            "last_error_message": webhook.last_error_message,
            "max_connections": webhook.max_connections,
        },
        "required_business_updates": [
            "business_connection",
            "business_message",
            "edited_business_message",
            "deleted_business_messages",
        ],
        "processed_updates": processed,
        "unresolved_failed_updates": failed,
        "connections": {
            "total": len(connections),
            "active": sum(1 for item in connections if item.is_active),
            "granted_rights": dict(rights_counter),
            "items": connection_items[:100],
        },
        "archive": {
            "dialogs": int(await session.scalar(select(func.count(Dialog.id))) or 0),
            "messages": int(await session.scalar(select(func.count(Message.id))) or 0),
            "messages_with_media": int(await session.scalar(
                select(func.count(func.distinct(Message.id))).join(Media, Media.message_id == Message.id)
            ) or 0),
            "media": int(await session.scalar(select(func.count(Media.id))) or 0),
            "media_statuses": media_statuses,
        },
    }
