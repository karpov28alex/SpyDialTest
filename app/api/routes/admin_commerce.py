from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy import exists, func, select

from app.api.routes.admin import AdminAuth, Session
from app.core.config import Settings, get_settings
from app.db.models import BusinessConnection, Payment, SubscriptionStatus, User

router = APIRouter(prefix="/api/admin/commerce", tags=["admin-commerce"])
PLANS_KEY = "phantom:commerce:plans"
COUPONS_KEY = "phantom:commerce:coupons"
AUDIT_KEY = "phantom:admin:audit"


def now() -> datetime:
    return datetime.now(UTC)


def redis_client(settings: Settings) -> Redis:
    return Redis.from_url(settings.redis_url, decode_responses=True)


async def audit(settings: Settings, admin: str, action: str, details: dict) -> None:
    client = redis_client(settings)
    await client.lpush(AUDIT_KEY, json.dumps({"at": now().isoformat(), "admin": admin, "action": action, "details": details}, ensure_ascii=False))
    await client.ltrim(AUDIT_KEY, 0, 1999)
    await client.aclose()


class PlanPatch(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    days: int = Field(ge=1, le=3650)
    price_rub: int = Field(ge=1, le=1_000_000)
    old_price_rub: int | None = Field(default=None, ge=1, le=1_000_000)
    badge: str = Field(default="", max_length=40)
    recurring: bool = True
    active: bool = True
    position: int = Field(default=100, ge=0, le=10000)


class CouponPatch(BaseModel):
    code: str = Field(min_length=2, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    title: str = Field(default="", max_length=100)
    discount_percent: int = Field(default=0, ge=0, le=100)
    discount_rub: int = Field(default=0, ge=0, le=1_000_000)
    bonus_days: int = Field(default=0, ge=0, le=3650)
    usage_limit: int = Field(default=0, ge=0, le=1_000_000)
    active: bool = True
    expires_at: datetime | None = None


class BulkAction(BaseModel):
    segment: Literal["all", "trial", "vip", "expired", "business", "no_business", "paid", "never_paid", "blocked"]
    action: Literal["grant_vip", "block", "unblock"]
    days: int = Field(default=0, ge=0, le=3650)
    reason: str = Field(default="", max_length=500)
    confirm_count: int = Field(ge=0)


async def read_hash(settings: Settings, key: str) -> list[dict]:
    client = redis_client(settings)
    raw = await client.hgetall(key)
    await client.aclose()
    items: list[dict] = []
    for item_id, value in raw.items():
        try:
            item = json.loads(value)
        except json.JSONDecodeError:
            continue
        item["id"] = item_id
        items.append(item)
    return items


def segment_conditions(segment: str, current: datetime) -> list:
    paid = exists(select(Payment.id).where(Payment.user_id == User.id, Payment.status == "paid"))
    business = exists(select(BusinessConnection.id).where(BusinessConnection.owner_user_id == User.id, BusinessConnection.is_active.is_(True)))
    if segment == "trial":
        return [User.subscription_status == SubscriptionStatus.trial, User.trial_ends_at > current]
    if segment == "vip":
        return [User.vip_ends_at.is_not(None), User.vip_ends_at > current]
    if segment == "expired":
        return [User.trial_ends_at <= current, (User.vip_ends_at.is_(None) | (User.vip_ends_at <= current)), User.is_access_disabled.is_(False)]
    if segment == "business":
        return [business]
    if segment == "no_business":
        return [~business]
    if segment == "paid":
        return [paid]
    if segment == "never_paid":
        return [~paid]
    if segment == "blocked":
        return [User.is_access_disabled.is_(True)]
    return []


@router.get("/plans")
async def plans(_: AdminAuth, settings: Settings = Depends(get_settings)) -> dict:
    items = await read_hash(settings, PLANS_KEY)
    items.sort(key=lambda x: (int(x.get("position", 100)), str(x.get("title", ""))))
    return {"items": items}


@router.put("/plans/{plan_id}")
async def save_plan(plan_id: str, body: PlanPatch, admin: AdminAuth, settings: Settings = Depends(get_settings)) -> dict:
    normalized = plan_id.strip()[:64] or secrets.token_hex(6)
    payload = {**body.model_dump(), "updated_at": now().isoformat(), "updated_by": admin}
    client = redis_client(settings)
    await client.hset(PLANS_KEY, normalized, json.dumps(payload, ensure_ascii=False))
    await client.aclose()
    await audit(settings, admin, "commerce.plan.saved", {"id": normalized, **body.model_dump()})
    return {"ok": True, "id": normalized, "plan": payload}


@router.delete("/plans/{plan_id}")
async def delete_plan(plan_id: str, admin: AdminAuth, settings: Settings = Depends(get_settings)) -> dict:
    client = redis_client(settings)
    deleted = await client.hdel(PLANS_KEY, plan_id)
    await client.aclose()
    await audit(settings, admin, "commerce.plan.deleted", {"id": plan_id})
    return {"ok": bool(deleted)}


@router.get("/coupons")
async def coupons(_: AdminAuth, settings: Settings = Depends(get_settings)) -> dict:
    items = await read_hash(settings, COUPONS_KEY)
    items.sort(key=lambda x: str(x.get("code", x.get("id", ""))))
    return {"items": items}


@router.put("/coupons/{coupon_id}")
async def save_coupon(coupon_id: str, body: CouponPatch, admin: AdminAuth, settings: Settings = Depends(get_settings)) -> dict:
    normalized = coupon_id.strip()[:64] or body.code.upper()
    payload = {**body.model_dump(mode="json"), "code": body.code.upper(), "used": 0, "updated_at": now().isoformat(), "updated_by": admin}
    client = redis_client(settings)
    previous = await client.hget(COUPONS_KEY, normalized)
    if previous:
        try:
            payload["used"] = int(json.loads(previous).get("used", 0))
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    await client.hset(COUPONS_KEY, normalized, json.dumps(payload, ensure_ascii=False))
    await client.aclose()
    await audit(settings, admin, "commerce.coupon.saved", {"id": normalized, "code": body.code.upper()})
    return {"ok": True, "id": normalized, "coupon": payload}


@router.delete("/coupons/{coupon_id}")
async def delete_coupon(coupon_id: str, admin: AdminAuth, settings: Settings = Depends(get_settings)) -> dict:
    client = redis_client(settings)
    deleted = await client.hdel(COUPONS_KEY, coupon_id)
    await client.aclose()
    await audit(settings, admin, "commerce.coupon.deleted", {"id": coupon_id})
    return {"ok": bool(deleted)}


@router.get("/segments/count")
async def segment_count(_: AdminAuth, session: Session, segment: str = Query("all")) -> dict:
    conditions = segment_conditions(segment, now())
    count = int(await session.scalar(select(func.count(User.id)).where(*conditions)) or 0)
    return {"segment": segment, "count": count}


@router.post("/bulk")
async def bulk_action(body: BulkAction, admin: AdminAuth, session: Session, settings: Settings = Depends(get_settings)) -> dict:
    current = now()
    conditions = segment_conditions(body.segment, current)
    users = list((await session.scalars(select(User).where(*conditions).order_by(User.id).limit(10000))).all())
    if len(users) != body.confirm_count:
        raise HTTPException(status_code=409, detail=f"Сегмент изменился: сейчас {len(users)} пользователей. Обновите расчёт и подтвердите снова.")
    if body.action == "grant_vip" and body.days < 1:
        raise HTTPException(status_code=422, detail="Для выдачи VIP укажите количество дней")
    for user in users:
        if body.action == "grant_vip":
            base = user.vip_ends_at if user.vip_ends_at and user.vip_ends_at > current else current
            user.vip_ends_at = base + timedelta(days=body.days)
            user.subscription_status = SubscriptionStatus.vip
            user.is_access_disabled = False
        elif body.action == "block":
            user.is_access_disabled = True
            user.subscription_status = SubscriptionStatus.disabled
        elif body.action == "unblock":
            user.is_access_disabled = False
            if user.vip_ends_at and user.vip_ends_at > current:
                user.subscription_status = SubscriptionStatus.vip
            elif user.trial_ends_at > current:
                user.subscription_status = SubscriptionStatus.trial
            else:
                user.subscription_status = SubscriptionStatus.expired
    await session.commit()
    await audit(settings, admin, "commerce.bulk.executed", {"segment": body.segment, "action": body.action, "days": body.days, "reason": body.reason, "count": len(users)})
    return {"ok": True, "affected": len(users), "segment": body.segment, "action": body.action}
