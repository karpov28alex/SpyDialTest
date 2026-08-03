from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import desc, func, or_, select

from app.api.routes.admin import AdminAuth, Session
from app.core.config import Settings, get_settings
from app.core.security import create_token, decode_token
from app.db.models import Dialog, Media, Message, MessageVersion, User
from app.services.media import safe_media_path
from app.services.media_backfill import backfill_media

router = APIRouter(prefix="/api/admin", tags=["admin-dialogs"])


def _clean_username(value: str | None) -> str | None:
    value = (value or "").strip().lstrip("@")
    return value or None


def _peer_key(dialog: Dialog) -> tuple[int, str, int]:
    if dialog.peer_telegram_id is not None:
        return dialog.owner_user_id, "peer", int(dialog.peer_telegram_id)
    return dialog.owner_user_id, "chat", int(dialog.telegram_chat_id)


def _peer_condition(dialog: Dialog):
    if dialog.peer_telegram_id is not None:
        return Dialog.peer_telegram_id == dialog.peer_telegram_id
    return Dialog.telegram_chat_id == dialog.telegram_chat_id


def _display_dialog(group: list[Dialog]) -> tuple[Dialog, str, str | None]:
    latest = max(
        group,
        key=lambda row: (
            row.last_message_at is not None,
            row.last_message_at,
            row.id,
        ),
    )
    username_row = next((row for row in group if _clean_username(row.peer_username)), None)
    name_row = next((row for row in group if (row.peer_name or "").strip()), None)
    identity = username_row or name_row or latest
    username = _clean_username(identity.peer_username)
    peer_id = identity.peer_telegram_id or latest.peer_telegram_id or latest.telegram_chat_id
    plain_name = (identity.peer_name or "").strip()
    display_name = plain_name or (f"@{username}" if username else f"ID {peer_id}")
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
    try:
        if not safe_media_path(settings, item.storage_key).is_file():
            return None
    except ValueError:
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
        headers={
            "Cache-Control": "private, max-age=300",
            "Accept-Ranges": "bytes",
        },
    )


@router.get("/dialog-viewer/dialogs")
async def all_dialogs(
    _: AdminAuth,
    session: Session,
    search: str = "",
    media_type: str = "all",
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict:
    """Return every saved conversation directly from existing DB tables."""
    dialogs = list(
        (
            await session.scalars(
                select(Dialog).order_by(desc(Dialog.last_message_at), desc(Dialog.id))
            )
        ).all()
    )
    if not dialogs:
        return {"items": [], "total": 0, "limit": limit, "offset": offset}

    groups: dict[tuple[int, str, int], list[Dialog]] = {}
    for dialog in dialogs:
        groups.setdefault(_peer_key(dialog), []).append(dialog)

    dialog_ids = [dialog.id for dialog in dialogs]
    count_rows = (
        await session.execute(
            select(Message.dialog_id, func.count(Message.id))
            .where(Message.dialog_id.in_(dialog_ids))
            .group_by(Message.dialog_id)
        )
    ).all()
    counts = {int(dialog_id): int(count) for dialog_id, count in count_rows}

    latest_messages = list(
        (
            await session.scalars(
                select(Message)
                .where(Message.dialog_id.in_(dialog_ids))
                .order_by(desc(Message.sent_at), desc(Message.id))
            )
        ).all()
    )
    latest_by_dialog: dict[int, Message] = {}
    for message in latest_messages:
        latest_by_dialog.setdefault(message.dialog_id, message)

    owners = {
        user.id: user
        for user in (
            await session.scalars(
                select(User).where(User.id.in_({dialog.owner_user_id for dialog in dialogs}))
            )
        ).all()
    }

    requested_media = media_type.strip().lower()
    media_dialog_ids: set[int] | None = None
    if requested_media and requested_media != "all":
        media_rows = (
            await session.execute(
                select(Message.dialog_id)
                .join(Media, Media.message_id == Message.id)
                .where(Media.media_type == requested_media)
                .distinct()
            )
        ).all()
        media_dialog_ids = {int(row[0]) for row in media_rows}

    term = search.strip().lower()
    items: list[dict] = []
    for group in groups.values():
        group_ids = [dialog.id for dialog in group]
        if media_dialog_ids is not None and not any(dialog_id in media_dialog_ids for dialog_id in group_ids):
            continue
        latest, display_name, username = _display_dialog(group)
        owner = owners.get(latest.owner_user_id)
        preview_candidates = [latest_by_dialog[dialog_id] for dialog_id in group_ids if dialog_id in latest_by_dialog]
        preview_message = max(preview_candidates, key=lambda row: (row.sent_at, row.id)) if preview_candidates else None
        messages_count = sum(counts.get(dialog_id, 0) for dialog_id in group_ids)
        owner_name = " ".join(
            part for part in (
                owner.first_name if owner else None,
                owner.last_name if owner else None,
            ) if part
        )
        haystack = " ".join(
            str(value or "")
            for value in (
                display_name,
                username,
                latest.peer_telegram_id,
                latest.telegram_chat_id,
                owner_name,
                owner.username if owner else None,
                owner.telegram_id if owner else None,
                preview_message.text if preview_message else None,
                preview_message.caption if preview_message else None,
            )
        ).lower()
        if term and term not in haystack:
            continue
        avatar_row = next((row for row in group if row.avatar), latest)
        preview = ""
        if preview_message:
            preview = preview_message.text or preview_message.caption or "[медиа]"
        items.append(
            {
                "id": latest.id,
                "dialog_ids": group_ids,
                "owner_user_id": latest.owner_user_id,
                "owner_telegram_id": owner.telegram_id if owner else None,
                "owner_username": owner.username if owner else None,
                "owner_name": owner_name,
                "display_name": display_name,
                "username": username,
                "peer_telegram_id": latest.peer_telegram_id,
                "telegram_chat_id": latest.telegram_chat_id,
                "avatar": avatar_row.avatar or latest.avatar,
                "last_message_at": (
                    preview_message.sent_at.isoformat()
                    if preview_message
                    else latest.last_message_at.isoformat() if latest.last_message_at else None
                ),
                "messages_count": messages_count,
                "preview": preview[:240],
                "preview_direction": preview_message.direction if preview_message else None,
                "preview_deleted": bool(preview_message.is_deleted) if preview_message else False,
                "connections_count": len({row.business_connection_id for row in group}),
            }
        )

    items.sort(key=lambda item: item["last_message_at"] or "", reverse=True)
    total = len(items)
    return {
        "items": items[offset : offset + limit],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/dialog-archive/users")
async def archive_users(
    _: AdminAuth,
    session: Session,
    search: str = "",
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict:
    last_message = func.max(Message.sent_at)
    query = (
        select(
            User,
            func.count(func.distinct(Dialog.id)).label("dialogs_count"),
            func.count(Message.id).label("messages_count"),
            last_message.label("last_message_at"),
        )
        .join(Dialog, Dialog.owner_user_id == User.id)
        .join(Message, Message.dialog_id == Dialog.id)
        .group_by(User.id)
    )
    term = search.strip()
    if term:
        conditions = [
            User.username.ilike(f"%{term}%"),
            User.first_name.ilike(f"%{term}%"),
            User.last_name.ilike(f"%{term}%"),
        ]
        if term.isdigit():
            conditions.append(User.telegram_id == int(term))
        query = query.where(or_(*conditions))
    rows = (
        await session.execute(
            query.order_by(desc(last_message)).offset(offset).limit(limit)
        )
    ).all()
    total_query = (
        select(func.count(func.distinct(User.id)))
        .join(Dialog, Dialog.owner_user_id == User.id)
        .join(Message, Message.dialog_id == Dialog.id)
    )
    if term:
        conditions = [
            User.username.ilike(f"%{term}%"),
            User.first_name.ilike(f"%{term}%"),
            User.last_name.ilike(f"%{term}%"),
        ]
        if term.isdigit():
            conditions.append(User.telegram_id == int(term))
        total_query = total_query.where(or_(*conditions))
    total = int(await session.scalar(total_query) or 0)
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [
            {
                "id": user.id,
                "telegram_id": user.telegram_id,
                "username": user.username,
                "name": " ".join(part for part in (user.first_name, user.last_name) if part),
                "dialogs_count": int(dialogs_count or 0),
                "messages_count": int(messages_count or 0),
                "last_message_at": last_message_at.isoformat() if last_message_at else None,
            }
            for user, dialogs_count, messages_count, last_message_at in rows
        ],
    }


@router.get("/media/archive-stats")
async def media_archive_stats(
    _: AdminAuth,
    session: Session,
    settings: Settings = Depends(get_settings),
) -> dict:
    rows = list((await session.scalars(select(Media))).all())
    downloaded = missing = pending = failed = 0
    for item in rows:
        if item.download_status == "downloaded" and item.storage_key:
            try:
                exists = safe_media_path(settings, item.storage_key).is_file()
            except ValueError:
                exists = False
            if exists:
                downloaded += 1
            else:
                missing += 1
        elif item.download_status in {"failed", "error"}:
            failed += 1
        else:
            pending += 1
    return {
        "total": len(rows),
        "downloaded": downloaded,
        "missing": missing,
        "pending": pending,
        "failed": failed,
    }


@router.post("/media/backfill")
async def run_media_backfill(
    _: AdminAuth,
    session: Session,
    settings: Settings = Depends(get_settings),
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    return await backfill_media(
        session,
        settings,
        limit=limit,
        include_missing_files=True,
    )


@router.get("/users/{user_id}/dialogs")
async def user_dialogs(user_id: int, _: AdminAuth, session: Session) -> dict:
    rows = list(
        (
            await session.scalars(
                select(Dialog)
                .where(Dialog.owner_user_id == user_id)
                .order_by(desc(Dialog.last_message_at), desc(Dialog.id))
            )
        ).all()
    )
    groups: dict[tuple[int, str, int], list[Dialog]] = {}
    for row in rows:
        groups.setdefault(_peer_key(row), []).append(row)
    items = []
    for group in groups.values():
        dialog_ids = [row.id for row in group]
        count = int(
            await session.scalar(
                select(func.count(Message.id)).where(Message.dialog_id.in_(dialog_ids))
            )
            or 0
        )
        latest, display_name, username = _display_dialog(group)
        avatar_row = next((row for row in group if row.avatar), latest)
        items.append(
            {
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
            }
        )
    items.sort(key=lambda item: item["last_message_at"] or "", reverse=True)
    return {"items": items}


@router.get("/dialogs/{dialog_id}/messages")
async def dialog_messages(
    dialog_id: int,
    _: AdminAuth,
    session: Session,
    settings: Settings = Depends(get_settings),
    limit: int = Query(100, ge=1, le=500),
    before_id: int | None = Query(None, ge=1),
) -> dict:
    dialog = await session.get(Dialog, dialog_id)
    if not dialog:
        raise HTTPException(status_code=404, detail="Dialog not found")
    matching_dialogs = list(
        (
            await session.scalars(
                select(Dialog).where(
                    Dialog.owner_user_id == dialog.owner_user_id,
                    _peer_condition(dialog),
                )
            )
        ).all()
    ) or [dialog]
    dialog_ids = [row.id for row in matching_dialogs]
    query = select(Message).where(Message.dialog_id.in_(dialog_ids))
    if before_id:
        query = query.where(Message.id < before_id)
    newest_first = list(
        (
            await session.scalars(
                query.order_by(desc(Message.sent_at), desc(Message.id)).limit(limit + 1)
            )
        ).all()
    )
    has_more = len(newest_first) > limit
    page = newest_first[:limit]
    rows = list(reversed(page))
    result = []
    for message in rows:
        versions = list(
            (
                await session.scalars(
                    select(MessageVersion)
                    .where(MessageVersion.message_id == message.id)
                    .order_by(MessageVersion.version_number)
                )
            ).all()
        )
        media = list(
            (
                await session.scalars(
                    select(Media)
                    .where(Media.message_id == message.id)
                    .order_by(Media.id)
                )
            ).all()
        )
        result.append(
            {
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
                "versions": [
                    {
                        "number": version.version_number,
                        "text": version.text,
                        "caption": version.caption,
                        "created_at": version.created_at.isoformat(),
                    }
                    for version in versions
                ] if message.edited_at else [],
                "media": [_serialize_media(item, settings) for item in media],
            }
        )
    latest, display_name, username = _display_dialog(matching_dialogs)
    avatar_row = next((row for row in matching_dialogs if row.avatar), latest)
    total = int(
        await session.scalar(
            select(func.count(Message.id)).where(Message.dialog_id.in_(dialog_ids))
        )
        or 0
    )
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
            "owner_user_id": latest.owner_user_id,
        },
        "items": result,
        "total": total,
        "has_more": has_more,
        "next_before_id": rows[0].id if has_more and rows else None,
    }
