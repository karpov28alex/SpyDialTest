from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select

from app.api.deps import CurrentUser, SessionDep
from app.core.config import Settings, get_settings
from app.core.security import create_token, decode_token
from app.db.models import BusinessConnection, Dialog, Media, Message, MessageVersion, User
from app.db.session import get_session
from app.services.access import access_state, get_monetization_settings, payment_plans
from app.services.access_funnel import channel_verified, get_funnel_config
from app.services.media import safe_media_path
from app.services.users import referral_code

router = APIRouter(prefix="/api", tags=["user"])


def avatar_url(user_id: int, dialog_id: int, settings: Settings) -> str:
    token = create_token(f"{user_id}:{dialog_id}", "dialog_avatar", timedelta(minutes=15), settings)
    return f"/api/avatar/{token}"


async def require_archive_access(user: User, session: SessionDep) -> None:
    funnel = await get_funnel_config()
    if funnel.enabled and funnel.channel_required and not await channel_verified(user.telegram_id):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "CHANNEL_SUBSCRIPTION_REQUIRED",
                "message": funnel.subscription_text,
                "channel_url": funnel.channel_url,
            },
        )
    access = await access_state(session, user)
    if funnel.enabled and not access.active:
        raise HTTPException(
            status_code=402,
            detail={
                "code": "PAYMENT_REQUIRED",
                "message": funnel.referral_text if user.referral_bonus_granted_at is None else funnel.payment_required_text,
                "payment_url": funnel.payment_url,
                "payment_button_text": funnel.payment_button_text,
                "referral_available": user.referral_bonus_granted_at is None,
            },
        )


@router.get("/me")
async def me(user: CurrentUser, session: SessionDep, settings: Settings = Depends(get_settings)) -> dict:
    connection = await session.scalar(select(BusinessConnection).where(BusinessConnection.owner_user_id == user.id, BusinessConnection.is_active.is_(True)))
    config = await get_monetization_settings(session)
    funnel = await get_funnel_config()
    access = await access_state(session, user)
    verified = await channel_verified(user.telegram_id)
    referral_link = f"https://t.me/{settings.telegram_bot_username}?start=ref_{referral_code(user)}"
    return {
        "id": user.id,
        "telegram_id": user.telegram_id,
        "username": user.username,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "business_connected": bool(connection),
        "access": {
            "active": access.active,
            "source": access.source,
            "ends_at": access.ends_at,
            "needs_payment": access.needs_payment,
        },
        "funnel": {
            "enabled": funnel.enabled,
            "channel_required": funnel.channel_required,
            "channel_verified": verified,
            "channel_title": funnel.channel_title,
            "channel_url": funnel.channel_url,
            "subscription_text": funnel.subscription_text,
            "referral_required": funnel.referral_required,
            "referral_text": funnel.referral_text,
            "payment_required_text": funnel.payment_required_text,
            "payment_button_text": funnel.payment_button_text,
            "payment_url": funnel.payment_url,
        },
        "monetization": {
            "free_trial_enabled": config.free_trial_enabled,
            "show_trial_in_profile": config.show_trial_in_profile,
            "show_tariffs": config.show_tariffs,
            "referral_available": user.referral_bonus_granted_at is None,
            "referral_link": referral_link,
            "payment_url": funnel.payment_url or config.payment_placeholder_url,
            "plans": payment_plans(config) if config.show_tariffs else [],
            "demo": True,
        },
    }


@router.get("/subscription")
async def subscription(user: CurrentUser, session: SessionDep, settings: Settings = Depends(get_settings)) -> dict:
    config = await get_monetization_settings(session)
    funnel = await get_funnel_config()
    access = await access_state(session, user)
    return {
        "access": {"active": access.active, "source": access.source, "ends_at": access.ends_at},
        "plans": payment_plans(config) if config.show_tariffs else [],
        "payment_url": funnel.payment_url or config.payment_placeholder_url,
        "payment_button_text": funnel.payment_button_text,
        "referral_link": f"https://t.me/{settings.telegram_bot_username}?start=ref_{referral_code(user)}",
        "referral_available": user.referral_bonus_granted_at is None,
        "demo": True,
    }


@router.get("/dialogs")
async def dialogs(user: CurrentUser, session: SessionDep, limit: int = Query(30, ge=1, le=100), cursor: int | None = None) -> dict:
    await require_archive_access(user, session)
    stmt = select(Dialog).where(Dialog.owner_user_id == user.id).order_by(desc(Dialog.last_message_at), desc(Dialog.id)).limit(limit + 1)
    if cursor:
        stmt = stmt.where(Dialog.id < cursor)
    rows = list((await session.scalars(stmt)).all())
    next_cursor = rows[-1].id if len(rows) > limit else None
    rows = rows[:limit]
    settings = get_settings()
    items = []
    for row in rows:
        last = await session.scalar(select(Message).where(Message.dialog_id == row.id).order_by(desc(Message.sent_at), desc(Message.id)).limit(1))
        count = await session.scalar(select(func.count(Message.id)).where(Message.dialog_id == row.id))
        items.append({
            "id": row.id, "peer_name": row.peer_name, "peer_username": row.peer_username,
            "avatar": avatar_url(user.id, row.id, settings) if row.peer_telegram_id else None,
            "message_count": int(count or 0), "last_message_at": row.last_message_at,
            "last_message_text": (last.text or last.caption) if last else None,
            "last_message_deleted": bool(last and last.is_deleted), "last_message_edited": bool(last and last.edited_at),
            "direction": last.direction if last else None, "is_hidden": row.is_hidden,
        })
    return {"items": items, "next_cursor": next_cursor}


@router.get("/dialogs/{dialog_id}")
async def dialog_detail(dialog_id: int, user: CurrentUser, session: SessionDep, limit: int = Query(50, ge=1, le=100), before_id: int | None = None) -> dict:
    await require_archive_access(user, session)
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
    settings = get_settings()
    for item in media_rows:
        token = None
        if item.download_status == "downloaded" and item.storage_key:
            token = create_token(f"{user.id}:{item.id}", "media_download", timedelta(seconds=settings.media_signing_ttl_seconds), settings)
        media_by_message.setdefault(item.message_id, []).append({
            "id": item.id, "type": item.media_type, "is_protected": item.is_protected,
            "status": item.download_status, "mime_type": item.mime_type, "filename": item.filename,
            "size": item.size, "url": f"/api/media/download/{token}" if token else None,
        })
    for version in version_rows:
        versions_by_message.setdefault(version.message_id, []).append({"version": version.version_number, "text": version.text, "caption": version.caption, "created_at": version.created_at})
    return {
        "dialog": {"id": dialog.id, "peer_name": dialog.peer_name, "peer_username": dialog.peer_username, "avatar": avatar_url(user.id, dialog.id, settings) if dialog.peer_telegram_id else None},
        "messages": [{
            "id": m.id, "direction": m.direction, "text": m.text, "caption": m.caption,
            "sent_at": m.sent_at, "edited_at": m.edited_at, "deleted_at": m.deleted_at,
            "is_deleted": m.is_deleted, "reply_to_message_id": m.reply_to_message_id,
            "media": media_by_message.get(m.id, []), "versions": versions_by_message.get(m.id, []),
        } for m in reversed(rows)],
        "next_cursor": next_cursor,
    }


@router.get("/media/download/{token}", include_in_schema=False)
async def download_media(token: str, session=Depends(get_session), settings: Settings = Depends(get_settings)):
    try:
        subject = decode_token(token, "media_download", settings)
        user_id_text, media_id_text = subject.split(":", 1)
        user_id, media_id = int(user_id_text), int(media_id_text)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=403, detail="Invalid media token") from exc
    owner = await session.get(User, user_id)
    if not owner:
        raise HTTPException(status_code=404, detail="User not found")
    await require_archive_access(owner, session)
    row = await session.execute(select(Media, Message, Dialog).join(Message, Message.id == Media.message_id).join(Dialog, Dialog.id == Message.dialog_id).where(Media.id == media_id, Dialog.owner_user_id == user_id))
    result = row.first()
    if not result:
        raise HTTPException(status_code=404, detail="Media not found")
    media, _, _ = result
    if media.download_status != "downloaded" or not media.storage_key:
        raise HTTPException(status_code=409, detail="Media is not ready")
    path = safe_media_path(settings, media.storage_key)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Media file missing")
    return FileResponse(path, media_type=media.mime_type or "application/octet-stream", filename=media.filename or f"media-{media.id}", headers={"Cache-Control": "private, no-store"})


class DialogPatch(BaseModel):
    is_hidden: bool | None = None
    is_muted: bool | None = None


@router.patch("/dialogs/{dialog_id}")
async def patch_dialog(dialog_id: int, body: DialogPatch, user: CurrentUser, session: SessionDep) -> dict:
    await require_archive_access(user, session)
    dialog = await session.scalar(select(Dialog).where(Dialog.id == dialog_id, Dialog.owner_user_id == user.id))
    if not dialog:
        raise HTTPException(status_code=404, detail="Dialog not found")
    for key, value in body.model_dump(exclude_none=True).items():
        setattr(dialog, key, value)
    await session.commit()
    return {"ok": True}


@router.get("/messages/{message_id}/versions")
async def versions(message_id: int, user: CurrentUser, session: SessionDep) -> dict:
    await require_archive_access(user, session)
    message = await session.scalar(select(Message).join(Dialog).where(Message.id == message_id, Dialog.owner_user_id == user.id))
    if not message:
        raise HTTPException(status_code=404, detail="Message not found")
    rows = list((await session.scalars(select(MessageVersion).where(MessageVersion.message_id == message.id).order_by(MessageVersion.version_number))).all())
    return {"items": [{"version": r.version_number, "text": r.text, "caption": r.caption, "created_at": r.created_at} for r in rows], "current": {"text": message.text, "caption": message.caption, "edited_at": message.edited_at}}


SETTINGS_FIELDS = ("notifications_enabled", "save_protected_media", "notify_edits", "notify_deletions", "notify_protected_media", "notify_connection", "hide_preview", "notify_emoji", "theme", "language", "timezone")


@router.get("/settings")
async def get_settings_route(user: CurrentUser) -> dict:
    return {key: getattr(user.settings, key) for key in SETTINGS_FIELDS}


class SettingsPatch(BaseModel):
    notifications_enabled: bool | None = None
    save_protected_media: bool | None = None
    notify_edits: bool | None = None
    notify_deletions: bool | None = None
    notify_protected_media: bool | None = None
    notify_connection: bool | None = None
    hide_preview: bool | None = None
    notify_emoji: bool | None = None
    theme: str | None = Field(default=None, pattern="^(dark|light|system)$")
    language: str | None = Field(default=None, max_length=16)
    timezone: str | None = Field(default=None, max_length=64)


@router.patch("/settings")
async def patch_settings(body: SettingsPatch, user: CurrentUser, session: SessionDep) -> dict:
    values = body.model_dump(exclude_none=True)
    for key, value in values.items():
        setattr(user.settings, key, value)
    await session.commit()
    return {"ok": True, "settings": {key: getattr(user.settings, key) for key in SETTINGS_FIELDS}}
