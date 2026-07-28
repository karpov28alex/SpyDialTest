from __future__ import annotations

import hmac
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.security import create_token, decode_token
from app.db.models import (
    BusinessConnection,
    Dialog,
    FailedUpdate,
    Media,
    Message,
    MessageVersion,
    Payment,
    SubscriptionStatus,
    User,
)
from app.db.session import get_session

router = APIRouter(prefix="/api/admin", tags=["admin"])


class AdminLoginRequest(BaseModel):
    email: EmailStr
    password: str


async def admin_guard(
    authorization: Annotated[str | None, Header()] = None,
    settings: Settings = Depends(get_settings),
) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    try:
        subject = decode_token(authorization.removeprefix("Bearer "), "admin_access", settings)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized") from exc
    if not hmac.compare_digest(subject, settings.admin_email):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    return subject


AdminAuth = Annotated[str, Depends(admin_guard)]
Session = Annotated[AsyncSession, Depends(get_session)]


@router.post("/auth/login")
async def login(payload: AdminLoginRequest, settings: Settings = Depends(get_settings)) -> dict:
    email_ok = hmac.compare_digest(payload.email.lower(), settings.admin_email.lower())
    password_ok = hmac.compare_digest(payload.password, settings.admin_password)
    if not (email_ok and password_ok):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    token = create_token(
        settings.admin_email,
        "admin_access",
        timedelta(minutes=settings.access_token_ttl_minutes),
        settings,
    )
    return {"access_token": token, "token_type": "bearer", "expires_in": settings.access_token_ttl_minutes * 60}


@router.get("/dashboard")
async def dashboard(_: AdminAuth, session: Session) -> dict:
    now = datetime.now(UTC)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month = today.replace(day=1)

    async def count(model, *conditions) -> int:
        return int(await session.scalar(select(func.count()).select_from(model).where(*conditions)) or 0)

    revenue_today = await session.scalar(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(Payment.status == "paid", Payment.paid_at >= today)
    )
    revenue_month = await session.scalar(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(Payment.status == "paid", Payment.paid_at >= month)
    )
    revenue_total = await session.scalar(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(Payment.status == "paid")
    )

    protected = await count(Media, Media.is_protected.is_(True))
    metrics = {
        "users_total": await count(User),
        "users_today": await count(User, User.registered_at >= today),
        "users_month": await count(User, User.registered_at >= month),
        "active_business": await count(BusinessConnection, BusinessConnection.is_active.is_(True)),
        "active_trial": await count(User, User.trial_ends_at > now, User.subscription_status == SubscriptionStatus.trial),
        "active_vip": await count(User, User.vip_ends_at.is_not(None), User.vip_ends_at > now),
        "dialogs": await count(Dialog),
        "messages": await count(Message),
        "edited_messages": await count(Message, Message.edited_at.is_not(None)),
        "deleted_messages": await count(Message, Message.is_deleted.is_(True)),
        "protected_media": protected,
        "failed_updates": await count(FailedUpdate, FailedUpdate.resolved.is_(False)),
        "revenue_today": float(revenue_today or 0),
        "revenue_month": float(revenue_month or 0),
        "revenue_total": float(revenue_total or 0),
    }

    start_day = today - timedelta(days=13)
    registration_rows = (
        await session.execute(
            select(func.date(User.registered_at).label("day"), func.count(User.id))
            .where(User.registered_at >= start_day)
            .group_by(func.date(User.registered_at))
            .order_by(func.date(User.registered_at))
        )
    ).all()
    event_rows = (
        await session.execute(
            select(
                func.date(Message.created_at).label("day"),
                func.count(Message.id),
                func.count(Message.id).filter(Message.edited_at.is_not(None)),
                func.count(Message.id).filter(Message.is_deleted.is_(True)),
            )
            .where(Message.created_at >= start_day)
            .group_by(func.date(Message.created_at))
            .order_by(func.date(Message.created_at))
        )
    ).all()

    recent_users = list(
        (
            await session.scalars(select(User).order_by(desc(User.registered_at)).limit(8))
        ).all()
    )
    return {
        "metrics": metrics,
        "registrations": [{"date": str(day), "count": count} for day, count in registration_rows],
        "events": [
            {"date": str(day), "messages": total, "edited": edited, "deleted": deleted}
            for day, total, edited, deleted in event_rows
        ],
        "recent_users": [serialize_user(user) for user in recent_users],
    }


def serialize_user(user: User) -> dict:
    return {
        "id": user.id,
        "telegram_id": user.telegram_id,
        "username": user.username,
        "name": " ".join(part for part in (user.first_name, user.last_name) if part),
        "registered_at": user.registered_at.isoformat(),
        "last_seen_at": user.last_seen_at.isoformat(),
        "trial_ends_at": user.trial_ends_at.isoformat(),
        "vip_ends_at": user.vip_ends_at.isoformat() if user.vip_ends_at else None,
        "subscription_status": user.subscription_status.value,
        "blocked": user.blocked_bot_at is not None,
    }


@router.get("/users")
async def users(
    _: AdminAuth,
    session: Session,
    search: str = "",
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    query = select(User)
    if search.strip():
        term = f"%{search.strip()}%"
        conditions = [
            User.username.ilike(term),
            User.first_name.ilike(term),
            User.last_name.ilike(term),
        ]
        if search.isdigit():
            conditions.append(User.telegram_id == int(search))
        from sqlalchemy import or_
        query = query.where(or_(*conditions))
    rows = list((await session.scalars(query.order_by(desc(User.registered_at)).offset(offset).limit(limit))).all())
    return {"items": [serialize_user(row) for row in rows], "limit": limit, "offset": offset}


@router.get("/users/{user_id}")
async def user_detail(user_id: int, _: AdminAuth, session: Session) -> dict:
    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    dialogs_count = await session.scalar(select(func.count(Dialog.id)).where(Dialog.owner_user_id == user.id))
    messages_count = await session.scalar(
        select(func.count(Message.id)).join(Dialog, Dialog.id == Message.dialog_id).where(Dialog.owner_user_id == user.id)
    )
    connections = list((await session.scalars(select(BusinessConnection).where(BusinessConnection.owner_user_id == user.id))).all())
    return {
        **serialize_user(user),
        "dialogs_count": int(dialogs_count or 0),
        "messages_count": int(messages_count or 0),
        "connections": [
            {"id": row.id, "active": row.is_active, "connected_at": row.connected_at.isoformat(), "last_activity_at": row.last_activity_at.isoformat() if row.last_activity_at else None}
            for row in connections
        ],
    }


@router.get("/users/{user_id}/dialogs")
async def user_dialogs(user_id: int, _: AdminAuth, session: Session) -> dict:
    rows = list((await session.scalars(select(Dialog).where(Dialog.owner_user_id == user_id).order_by(desc(Dialog.last_message_at)))).all())
    items = []
    for row in rows:
        count = await session.scalar(select(func.count(Message.id)).where(Message.dialog_id == row.id))
        items.append({
            "id": row.id,
            "name": row.peer_name,
            "username": row.peer_username,
            "telegram_chat_id": row.telegram_chat_id,
            "avatar": row.avatar,
            "last_message_at": row.last_message_at.isoformat() if row.last_message_at else None,
            "messages_count": int(count or 0),
        })
    return {"items": items}


@router.get("/dialogs/{dialog_id}/messages")
async def dialog_messages(
    dialog_id: int,
    _: AdminAuth,
    session: Session,
    limit: int = Query(100, ge=1, le=500),
) -> dict:
    dialog = await session.get(Dialog, dialog_id)
    if not dialog:
        raise HTTPException(status_code=404, detail="Dialog not found")
    rows = list((await session.scalars(select(Message).where(Message.dialog_id == dialog_id).order_by(desc(Message.sent_at)).limit(limit))).all())
    result = []
    for message in rows:
        versions = list((await session.scalars(select(MessageVersion).where(MessageVersion.message_id == message.id).order_by(MessageVersion.version_number))).all())
        media = list((await session.scalars(select(Media).where(Media.message_id == message.id))).all())
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
            "versions": [{"number": v.version_number, "text": v.text, "caption": v.caption, "created_at": v.created_at.isoformat()} for v in versions],
            "media": [{"id": m.id, "type": m.media_type, "protected": m.is_protected, "status": m.download_status, "filename": m.filename, "size": m.size} for m in media],
        })
    return {"dialog": {"id": dialog.id, "name": dialog.peer_name, "username": dialog.peer_username}, "items": result}


@router.get("/protected-media")
async def protected_media(_: AdminAuth, session: Session, limit: int = Query(100, ge=1, le=500)) -> dict:
    rows = (
        await session.execute(
            select(Media, Message, Dialog)
            .join(Message, Message.id == Media.message_id)
            .join(Dialog, Dialog.id == Message.dialog_id)
            .where(Media.is_protected.is_(True))
            .order_by(desc(Media.created_at))
            .limit(limit)
        )
    ).all()
    return {"items": [
        {
            "id": media.id,
            "type": media.media_type,
            "filename": media.filename,
            "size": media.size,
            "status": media.download_status,
            "created_at": media.created_at.isoformat(),
            "dialog_id": dialog.id,
            "dialog_name": dialog.peer_name,
            "message_id": message.id,
        }
        for media, message, dialog in rows
    ]}
