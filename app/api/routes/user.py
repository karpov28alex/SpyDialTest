from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, select

from app.api.deps import CurrentUser, SessionDep
from app.db.models import BusinessConnection, Dialog, Media, Message, MessageVersion
from app.services.access import access_ends_at, refresh_subscription_status

router = APIRouter(prefix="/api", tags=["user"])


@router.get("/me")
async def me(user: CurrentUser, session: SessionDep) -> dict:
    connection = await session.scalar(select(BusinessConnection).where(BusinessConnection.owner_user_id == user.id, BusinessConnection.is_active.is_(True)))
    return {
        "id": user.id,
        "telegram_id": user.telegram_id,
        "username": user.username,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "subscription_status": refresh_subscription_status(user).value,
        "access_ends_at": access_ends_at(user),
        "business_connected": bool(connection),
    }


@router.get("/dialogs")
async def dialogs(user: CurrentUser, session: SessionDep, limit: int = Query(30, ge=1, le=100), cursor: int | None = None) -> dict:
    stmt = select(Dialog).where(Dialog.owner_user_id == user.id).order_by(desc(Dialog.last_message_at), desc(Dialog.id)).limit(limit + 1)
    if cursor:
        stmt = stmt.where(Dialog.id < cursor)
    rows = list((await session.scalars(stmt)).all())
    next_cursor = rows[-1].id if len(rows) > limit else None
    rows = rows[:limit]
    items = []
    for row in rows:
        last = await session.scalar(select(Message).where(Message.dialog_id == row.id).order_by(desc(Message.sent_at), desc(Message.id)).limit(1))
        items.append({
            "id": row.id,
            "peer_name": row.peer_name,
            "peer_username": row.peer_username,
            "last_message_at": row.last_message_at,
            "last_message_text": (last.text or last.caption) if last else None,
            "last_message_deleted": bool(last and last.is_deleted),
            "last_message_edited": bool(last and last.edited_at),
            "direction": last.direction if last else None,
            "is_hidden": row.is_hidden,
        })
    return {"items": items, "next_cursor": next_cursor}


@router.get("/dialogs/{dialog_id}")
async def dialog_detail(dialog_id: int, user: CurrentUser, session: SessionDep, limit: int = Query(50, ge=1, le=100), before_id: int | None = None) -> dict:
    dialog = await session.scalar(select(Dialog).where(Dialog.id == dialog_id, Dialog.owner_user_id == user.id))
    if not dialog:
        raise HTTPException(status_code=404, detail="Dialog not found")
    stmt = select(Message).where(Message.dialog_id == dialog.id).order_by(desc(Message.id)).limit(limit + 1)
    if before_id:
        stmt = stmt.where(Message.id < before_id)
    rows = list((await session.scalars(stmt)).all())
    next_cursor = rows[-1].id if len(rows) > limit else None
    rows = rows[:limit]
    ids = [m.id for m in rows]
    media_rows = list((await session.scalars(select(Media).where(Media.message_id.in_(ids or [-1])))).all())
    version_rows = list((await session.scalars(select(MessageVersion).where(MessageVersion.message_id.in_(ids or [-1])).order_by(MessageVersion.message_id, MessageVersion.version_number))).all())
    media_by_message: dict[int, list[dict]] = {}
    versions_by_message: dict[int, list[dict]] = {}
    for item in media_rows:
        media_by_message.setdefault(item.message_id, []).append({"id": item.id, "type": item.media_type, "is_protected": item.is_protected, "status": item.download_status})
    for version in version_rows:
        versions_by_message.setdefault(version.message_id, []).append({"version": version.version_number, "text": version.text, "caption": version.caption, "created_at": version.created_at})
    return {
        "dialog": {"id": dialog.id, "peer_name": dialog.peer_name, "peer_username": dialog.peer_username},
        "messages": [{
            "id": m.id,
            "direction": m.direction,
            "text": m.text,
            "caption": m.caption,
            "sent_at": m.sent_at,
            "edited_at": m.edited_at,
            "deleted_at": m.deleted_at,
            "is_deleted": m.is_deleted,
            "reply_to_message_id": m.reply_to_message_id,
            "media": media_by_message.get(m.id, []),
            "versions": versions_by_message.get(m.id, []),
        } for m in reversed(rows)],
        "next_cursor": next_cursor,
    }


class DialogPatch(BaseModel):
    is_hidden: bool | None = None
    is_muted: bool | None = None


@router.patch("/dialogs/{dialog_id}")
async def patch_dialog(dialog_id: int, body: DialogPatch, user: CurrentUser, session: SessionDep) -> dict:
    dialog = await session.scalar(select(Dialog).where(Dialog.id == dialog_id, Dialog.owner_user_id == user.id))
    if not dialog:
        raise HTTPException(status_code=404, detail="Dialog not found")
    for key, value in body.model_dump(exclude_none=True).items():
        setattr(dialog, key, value)
    await session.commit()
    return {"ok": True}


@router.get("/messages/{message_id}/versions")
async def versions(message_id: int, user: CurrentUser, session: SessionDep) -> dict:
    message = await session.scalar(select(Message).join(Dialog).where(Message.id == message_id, Dialog.owner_user_id == user.id))
    if not message:
        raise HTTPException(status_code=404, detail="Message not found")
    rows = list((await session.scalars(select(MessageVersion).where(MessageVersion.message_id == message.id).order_by(MessageVersion.version_number))).all())
    return {"items": [{"version": r.version_number, "text": r.text, "caption": r.caption, "created_at": r.created_at} for r in rows], "current": {"text": message.text, "caption": message.caption, "edited_at": message.edited_at}}


SETTINGS_FIELDS = ("notify_edits", "notify_deletions", "notify_protected_media", "notify_connection", "hide_preview", "notify_emoji", "theme", "language", "timezone")


@router.get("/settings")
async def get_settings_route(user: CurrentUser) -> dict:
    return {key: getattr(user.settings, key) for key in SETTINGS_FIELDS}


class SettingsPatch(BaseModel):
    notify_edits: bool | None = None
    notify_deletions: bool | None = None
    notify_protected_media: bool | None = None
    notify_connection: bool | None = None
    hide_preview: bool | None = None
    notify_emoji: bool | None = None
    theme: str | None = Field(default=None, pattern="^(dark|light|system)$")
    language: str | None = None
    timezone: str | None = None


@router.patch("/settings")
async def patch_settings(body: SettingsPatch, user: CurrentUser, session: SessionDep) -> dict:
    for key, value in body.model_dump(exclude_none=True).items():
        setattr(user.settings, key, value)
    await session.commit()
    return {"ok": True, "settings": {key: getattr(user.settings, key) for key in SETTINGS_FIELDS}}
