from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import desc, func, select

from app.api.routes.admin import AdminAuth, Session
from app.core.config import Settings, get_settings
from app.core.security import create_token, decode_token
from app.db.models import Dialog, Media, Message, MessageVersion
from app.services.media import safe_media_path

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _peer_condition(dialog: Dialog):
    if dialog.peer_telegram_id is not None:
        return Dialog.peer_telegram_id == dialog.peer_telegram_id
    return Dialog.telegram_chat_id == dialog.telegram_chat_id


def _clean_username(value: str | None) -> str | None:
    value = (value or "").strip().lstrip("@")
    return value or None


def _display_dialog(group: list[Dialog]) -> tuple[Dialog, str, str | None]:
    latest = max(group, key=lambda row: (row.last_message_at is not None, row.last_message_at, row.id))
    username_row = next((row for row in group if _clean_username(row.peer_username)), None)
    name_row = next((row for row in group if (row.peer_name or "").strip()), None)
    identity = username_row or name_row or latest
    username = _clean_username(identity.peer_username)
    peer_id = identity.peer_telegram_id or latest.peer_telegram_id or latest.telegram_chat_id
    plain_name = (identity.peer_name or "").strip()
    display_name = f"@{username}" if username else (plain_name or f"ID {peer_id}")
    return latest, display_name, username


def _media_kind(item: Media) -> str:
    raw = (item.media_type or "").strip().lower().replace("-", "_")
    mime = (item.mime_type or "").lower()
    aliases = {
        "voice_message": "voice",
        "voice_note": "voice",
        "video_message": "video_note",
        "round_video": "video_note",
        "video_circle": "video_note",
        "gif": "animation",
        "image": "photo",
        "file": "document",
    }
    raw = aliases.get(raw, raw)
    if raw:
        return raw
    if mime.startswith("image/"):
        return "photo"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    return "document"


def _stream_url(item: Media, settings: Settings) -> str | None:
    if item.download_status != "downloaded" or not item.storage_key:
        return None
    token = create_token(
        str(item.id),
        "admin_media_download",
        timedelta(seconds=settings.media_signing_ttl_seconds),
        settings,
    )
    return f"/api/admin/media/stream/{token}"


def _serialize_media(item: Media, settings: Settings) -> dict:
    return {
        "id": item.id,
        "type": item.media_type,
        "kind": _media_kind(item),
        "mime_type": item.mime_type,
        "protected": item.is_protected,
        "status": item.download_status,
        "filename": item.filename,
        "size": item.size,
        "duration": item.duration,
        "width": item.width,
        "height": item.height,
        "url": _stream_url(item, settings),
    }


@router.get("/media/stream/{token}", include_in_schema=False)
async def stream_admin_media(
    token: str,
    session: Session,
    settings: Settings = Depends(get_settings),
):
    try:
        media_id = int(decode_token(token, "admin_media_download", settings))
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=403, detail="Invalid media token") from exc
    media = await session.get(Media, media_id)
    if not media or media.download_status != "downloaded" or not media.storage_key:
        raise HTTPException(status_code=404, detail="Media not found")
    path = safe_media_path(settings, media.storage_key)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Media file missing")
    return FileResponse(
        path,
        media_type=media.mime_type or "application/octet-stream",
        filename=media.filename or f"media-{media.id}",
        content_disposition_type="inline",
        headers={"Cache-Control": "private, max-age=300"},
    )


@router.get("/users/{user_id}/dialogs")
async def user_dialogs(user_id: int, _: AdminAuth, session: Session) -> dict:
    rows = list((await session.scalars(
        select(Dialog)
        .where(Dialog.owner_user_id == user_id)
        .order_by(desc(Dialog.last_message_at), desc(Dialog.id))
    )).all())
    groups: dict[tuple[str, int], list[Dialog]] = {}
    for row in rows:
        peer_value = row.peer_telegram_id if row.peer_telegram_id is not None else row.telegram_chat_id
        key = ("peer" if row.peer_telegram_id is not None else "chat", int(peer_value))
        groups.setdefault(key, []).append(row)

    items = []
    for group in groups.values():
        dialog_ids = [row.id for row in group]
        count = int(await session.scalar(
            select(func.count(Message.id)).where(Message.dialog_id.in_(dialog_ids))
        ) or 0)
        latest, display_name, username = _display_dialog(group)
        avatar_row = next((row for row in group if row.avatar), latest)
        items.append({
            "id": latest.id,
            "dialog_ids": dialog_ids,
            "name": display_name,
            "display_name": display_name,
            "username": username,
            "telegram_chat_id": latest.telegram_chat_id,
            "peer_telegram_id": latest.peer_telegram_id,
            "avatar": avatar_row.avatar or latest.avatar,
            "last_message_at": latest.last_message_at.isoformat() if latest.last_message_at else None,
            "messages_count": count,
            "is_hidden": all(row.is_hidden for row in group),
            "connections_count": len({row.business_connection_id for row in group}),
        })
    items.sort(key=lambda item: item["last_message_at"] or "", reverse=True)
    return {"items": items}


@router.get("/dialogs/{dialog_id}/messages")
async def dialog_messages(
    dialog_id: int,
    _: AdminAuth,
    session: Session,
    settings: Settings = Depends(get_settings),
    limit: int = Query(500, ge=1, le=1000),
) -> dict:
    dialog = await session.get(Dialog, dialog_id)
    if not dialog:
        raise HTTPException(status_code=404, detail="Dialog not found")

    matching_dialogs = list((await session.scalars(
        select(Dialog).where(
            Dialog.owner_user_id == dialog.owner_user_id,
            _peer_condition(dialog),
        )
    )).all())
    if not matching_dialogs:
        matching_dialogs = [dialog]
    dialog_ids = [row.id for row in matching_dialogs]
    rows = list((await session.scalars(
        select(Message)
        .where(Message.dialog_id.in_(dialog_ids))
        .order_by(Message.sent_at, Message.id)
        .limit(limit)
    )).all())

    result = []
    for message in rows:
        versions = list((await session.scalars(
            select(MessageVersion)
            .where(MessageVersion.message_id == message.id)
            .order_by(MessageVersion.version_number)
        )).all())
        media = list((await session.scalars(
            select(Media).where(Media.message_id == message.id).order_by(Media.id)
        )).all())
        result.append({
            "id": message.id,
            "telegram_message_id": message.telegram_message_id,
            "direction": message.direction,
            "text": message.text,
            "caption": message.caption,
            "sent_at": message.sent_at.isoformat(),
            "edited_at": message.edited_at.isoformat() if message.edited_at else None,
            "deleted_at": message.deleted_at.isoformat() if message.deleted_at else None,
            "is_deleted": message.is_deleted,
            "reply_to_message_id": message.reply_to_message_id,
            "versions": [{
                "number": version.version_number,
                "text": version.text,
                "caption": version.caption,
                "created_at": version.created_at.isoformat(),
            } for version in versions] if message.edited_at else [],
            "media": [_serialize_media(item, settings) for item in media],
        })

    latest, display_name, username = _display_dialog(matching_dialogs)
    avatar_row = next((row for row in matching_dialogs if row.avatar), latest)
    return {
        "dialog": {
            "id": dialog.id,
            "dialog_ids": dialog_ids,
            "name": display_name,
            "display_name": display_name,
            "username": username,
            "peer_telegram_id": latest.peer_telegram_id,
            "telegram_chat_id": latest.telegram_chat_id,
            "avatar": avatar_row.avatar or latest.avatar,
        },
        "items": result,
    }
