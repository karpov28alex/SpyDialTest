from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, select

from app.api.routes.admin import AdminAuth, Session
from app.core.config import Settings, get_settings
from app.core.security import create_token
from app.db.models import Dialog, Media, Message, User

router = APIRouter(prefix="/api/admin/explorer", tags=["admin-explorer"])


def _media_url(media: Media, settings: Settings) -> str | None:
    if media.download_status != "downloaded" or not media.storage_key:
        return None
    token = create_token(
        str(media.id),
        "admin_media_download",
        timedelta(seconds=settings.media_signing_ttl_seconds),
        settings,
    )
    return f"/api/admin/media/download/{token}"


@router.get("/protected-media")
async def protected_media(
    _: AdminAuth,
    session: Session,
    settings: Settings = Depends(get_settings),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict:
    rows = (
        await session.execute(
            select(Media, Message, Dialog, User)
            .join(Message, Message.id == Media.message_id)
            .join(Dialog, Dialog.id == Message.dialog_id)
            .join(User, User.id == Dialog.owner_user_id)
            .where(Media.is_protected.is_(True))
            .order_by(desc(Media.created_at), desc(Media.id))
            .offset(offset)
            .limit(limit)
        )
    ).all()
    return {
        "items": [
            {
                "id": media.id,
                "type": media.media_type,
                "filename": media.filename,
                "mime_type": media.mime_type,
                "size": media.size,
                "status": media.download_status,
                "created_at": media.created_at.isoformat(),
                "downloaded_at": media.downloaded_at.isoformat() if media.downloaded_at else None,
                "available_to_user": media.download_status == "downloaded" and bool(media.storage_key),
                "dialog": {
                    "id": dialog.id,
                    "name": dialog.peer_name,
                    "username": dialog.peer_username,
                    "avatar": dialog.avatar,
                },
                "owner": {
                    "id": owner.id,
                    "telegram_id": owner.telegram_id,
                    "username": owner.username,
                    "name": " ".join(part for part in (owner.first_name, owner.last_name) if part),
                },
                "message_id": message.id,
                "url": _media_url(media, settings),
            }
            for media, message, dialog, owner in rows
        ],
        "limit": limit,
        "offset": offset,
    }
