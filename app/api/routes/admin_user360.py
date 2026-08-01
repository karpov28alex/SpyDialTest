from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from app.api.routes.admin import AdminAuth, Session, serialize_user
from app.db.models import (
    BusinessConnection,
    Dialog,
    Media,
    Message,
    Payment,
    Referral,
    Subscription,
    SubscriptionStatus,
    User,
    UserSettings,
)

router = APIRouter(prefix="/api/admin/user360", tags=["admin-user360"])


class SettingsPatch(BaseModel):
    notifications_enabled: bool | None = None
    save_protected_media: bool | None = None
    notify_edits: bool | None = None
    notify_deletions: bool | None = None
    notify_protected_media: bool | None = None
    notify_connection: bool | None = None
    hide_preview: bool | None = None
    notify_emoji: bool | None = None
    language: str | None = Field(default=None, max_length=16)
    timezone: str | None = Field(default=None, max_length=64)
    theme: str | None = Field(default=None, max_length=16)


class AccessPatch(BaseModel):
    days: int = Field(ge=1, le=3650)
    kind: str = Field(default="vip", pattern="^(vip|trial)$")


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


@router.get("/search")
async def search_users(
    _: AdminAuth,
    session: Session,
    q: str = Query("", min_length=0, max_length=160),
    limit: int = Query(30, ge=1, le=100),
) -> dict:
    term = q.strip()
    stmt = select(User)
    if term:
        like = f"%{term}%"
        conditions = [
            User.username.ilike(like),
            User.first_name.ilike(like),
            User.last_name.ilike(like),
        ]
        if term.isdigit():
            conditions.append(User.telegram_id == int(term))
        stmt = stmt.where(or_(*conditions))
    users = list((await session.scalars(stmt.order_by(User.last_seen_at.desc()).limit(limit))).all())
    return {"items": [serialize_user(user) for user in users]}


@router.get("/users/{user_id}")
async def user_360(user_id: int, _: AdminAuth, session: Session) -> dict:
    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    settings = await session.get(UserSettings, user_id)
    connections = list(
        (
            await session.scalars(
                select(BusinessConnection)
                .where(BusinessConnection.owner_user_id == user_id)
                .order_by(BusinessConnection.connected_at.desc())
            )
        ).all()
    )
    payments = list(
        (
            await session.scalars(
                select(Payment)
                .where(Payment.user_id == user_id)
                .order_by(Payment.created_at.desc())
                .limit(100)
            )
        ).all()
    )
    subscriptions = list(
        (
            await session.scalars(
                select(Subscription)
                .where(Subscription.user_id == user_id)
                .order_by(Subscription.starts_at.desc())
                .limit(50)
            )
        ).all()
    )
    referrals = list(
        (
            await session.execute(
                select(Referral, User)
                .join(User, User.id == Referral.referred_user_id)
                .where(Referral.referrer_user_id == user_id)
                .order_by(Referral.joined_at.desc())
                .limit(100)
            )
        ).all()
    )

    dialogs_count = int(await session.scalar(select(func.count(Dialog.id)).where(Dialog.owner_user_id == user_id)) or 0)
    messages_count = int(
        await session.scalar(
            select(func.count(Message.id))
            .join(Dialog, Dialog.id == Message.dialog_id)
            .where(Dialog.owner_user_id == user_id)
        )
        or 0
    )
    edited_count = int(
        await session.scalar(
            select(func.count(Message.id))
            .join(Dialog, Dialog.id == Message.dialog_id)
            .where(Dialog.owner_user_id == user_id, Message.edited_at.is_not(None))
        )
        or 0
    )
    deleted_count = int(
        await session.scalar(
            select(func.count(Message.id))
            .join(Dialog, Dialog.id == Message.dialog_id)
            .where(Dialog.owner_user_id == user_id, Message.is_deleted.is_(True))
        )
        or 0
    )
    media_count = int(
        await session.scalar(
            select(func.count(Media.id))
            .join(Message, Message.id == Media.message_id)
            .join(Dialog, Dialog.id == Message.dialog_id)
            .where(Dialog.owner_user_id == user_id)
        )
        or 0
    )
    protected_count = int(
        await session.scalar(
            select(func.count(Media.id))
            .join(Message, Message.id == Media.message_id)
            .join(Dialog, Dialog.id == Message.dialog_id)
            .where(Dialog.owner_user_id == user_id, Media.is_protected.is_(True))
        )
        or 0
    )

    paid = [payment for payment in payments if payment.status == "paid"]
    paid_total = sum(float(payment.amount) for payment in paid)
    average_check = round(paid_total / len(paid), 2) if paid else 0

    timeline = [
        {"type": "registered", "title": "Регистрация", "at": _iso(user.registered_at)},
        {"type": "trial", "title": "Начало пробного доступа", "at": _iso(user.trial_started_at)},
    ]
    for connection in connections[:10]:
        timeline.append({
            "type": "business_connected" if connection.is_active else "business_disconnected",
            "title": "Business подключён" if connection.is_active else "Business отключён",
            "at": _iso(connection.connected_at if connection.is_active else connection.disconnected_at),
        })
    for payment in paid[:10]:
        timeline.append({"type": "payment", "title": f"Оплата {float(payment.amount):g} {payment.currency}", "at": _iso(payment.paid_at or payment.created_at)})
    for referral, referred in referrals[:10]:
        name = " ".join(filter(None, [referred.first_name, referred.last_name])) or referred.username or str(referred.telegram_id)
        timeline.append({"type": "referral", "title": f"Приглашён пользователь: {name}", "at": _iso(referral.joined_at)})
    timeline = sorted((item for item in timeline if item["at"]), key=lambda item: item["at"], reverse=True)[:30]

    return {
        "user": serialize_user(user) | {
            "language_code": user.language_code,
            "timezone": user.timezone,
            "trial_started_at": _iso(user.trial_started_at),
            "referrer_user_id": user.referrer_user_id,
            "access_disabled": user.is_access_disabled,
        },
        "metrics": {
            "dialogs": dialogs_count,
            "messages": messages_count,
            "edited": edited_count,
            "deleted": deleted_count,
            "media": media_count,
            "protected_media": protected_count,
            "payments": len(paid),
            "paid_total": round(paid_total, 2),
            "average_check": average_check,
            "referrals": len(referrals),
            "business_connections": len(connections),
            "active_business": sum(1 for connection in connections if connection.is_active),
        },
        "settings": {
            "notifications_enabled": settings.notifications_enabled if settings else True,
            "save_protected_media": settings.save_protected_media if settings else True,
            "notify_edits": settings.notify_edits if settings else True,
            "notify_deletions": settings.notify_deletions if settings else True,
            "notify_protected_media": settings.notify_protected_media if settings else True,
            "notify_connection": settings.notify_connection if settings else True,
            "hide_preview": settings.hide_preview if settings else False,
            "notify_emoji": settings.notify_emoji if settings else True,
            "language": settings.language if settings else "ru",
            "timezone": settings.timezone if settings else user.timezone,
            "theme": settings.theme if settings else "dark",
        },
        "connections": [{
            "id": connection.id,
            "telegram_connection_id": connection.telegram_connection_id,
            "business_user_id": connection.business_user_id,
            "active": connection.is_active,
            "connected_at": _iso(connection.connected_at),
            "disconnected_at": _iso(connection.disconnected_at),
            "last_activity_at": _iso(connection.last_activity_at),
            "rights": connection.rights,
            "disconnect_reason": connection.disconnect_reason,
        } for connection in connections],
        "payments": [{
            "id": payment.id,
            "amount": float(payment.amount),
            "currency": payment.currency,
            "status": payment.status,
            "provider": payment.provider,
            "external_id": payment.external_id,
            "recurring": payment.recurring,
            "created_at": _iso(payment.created_at),
            "paid_at": _iso(payment.paid_at),
            "refunded_at": _iso(payment.refunded_at),
        } for payment in payments],
        "subscriptions": [{
            "id": subscription.id,
            "status": subscription.status,
            "source": subscription.source,
            "starts_at": _iso(subscription.starts_at),
            "ends_at": _iso(subscription.ends_at),
        } for subscription in subscriptions],
        "referrals": [{
            "user": serialize_user(referred),
            "code": referral.code,
            "joined_at": _iso(referral.joined_at),
            "bonus_granted_at": _iso(referral.bonus_granted_at),
        } for referral, referred in referrals],
        "timeline": timeline,
    }


@router.patch("/users/{user_id}/settings")
async def patch_settings(user_id: int, body: SettingsPatch, _: AdminAuth, session: Session) -> dict:
    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    settings = await session.get(UserSettings, user_id)
    if not settings:
        settings = UserSettings(user_id=user_id)
        session.add(settings)
    for key, value in body.model_dump(exclude_none=True).items():
        setattr(settings, key, value)
    await session.commit()
    return {"ok": True}


@router.post("/users/{user_id}/grant")
async def grant_access(user_id: int, body: AccessPatch, _: AdminAuth, session: Session) -> dict:
    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    now = _now()
    if body.kind == "vip":
        base = user.vip_ends_at if user.vip_ends_at and user.vip_ends_at > now else now
        user.vip_ends_at = base + timedelta(days=body.days)
        user.subscription_status = SubscriptionStatus.vip
    else:
        base = user.trial_ends_at if user.trial_ends_at and user.trial_ends_at > now else now
        user.trial_ends_at = base + timedelta(days=body.days)
        user.subscription_status = SubscriptionStatus.trial
    user.is_access_disabled = False
    await session.commit()
    return {"ok": True, "status": user.subscription_status.value, "vip_ends_at": _iso(user.vip_ends_at), "trial_ends_at": _iso(user.trial_ends_at)}
