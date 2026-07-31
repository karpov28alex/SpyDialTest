from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy import and_, exists, func, or_, select

from app.api.routes.admin import AdminAuth, Session, serialize_user
from app.core.config import Settings, get_settings
from app.db.models import BusinessConnection, Payment, Referral, User

router = APIRouter(prefix="/api/admin/growth", tags=["admin-growth"])
CAMPAIGNS_KEY = "phantom:admin:campaigns"
AUDIT_KEY = "phantom:admin:audit"


class CampaignPatch(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    platform: str = Field(default="", max_length=120)
    cost_rub: float = Field(default=0, ge=0, le=100_000_000)
    note: str = Field(default="", max_length=1000)


def _now() -> datetime:
    return datetime.now(UTC)


def _redis(settings: Settings) -> Redis:
    return Redis.from_url(settings.redis_url, decode_responses=True)


async def _audit(settings: Settings, admin: str, action: str, details: dict) -> None:
    client = _redis(settings)
    payload = json.dumps({
        "at": _now().isoformat(),
        "admin": admin,
        "action": action,
        "details": details,
    }, ensure_ascii=False)
    await client.lpush(AUDIT_KEY, payload)
    await client.ltrim(AUDIT_KEY, 0, 999)
    await client.aclose()


@router.get("/campaigns")
async def campaigns(
    _: AdminAuth,
    session: Session,
    settings: Settings = Depends(get_settings),
    days: int = Query(30, ge=1, le=366),
) -> dict:
    start = _now().replace(tzinfo=None) - timedelta(days=days - 1)
    client = _redis(settings)
    raw_meta = await client.hgetall(CAMPAIGNS_KEY)
    await client.aclose()
    metadata = {code: json.loads(value) for code, value in raw_meta.items()}

    rows = (await session.execute(
        select(
            Referral.code,
            func.count(func.distinct(Referral.referred_user_id)).label("registrations"),
            func.count(func.distinct(Payment.user_id)).filter(Payment.status == "paid").label("buyers"),
            func.coalesce(func.sum(Payment.amount).filter(Payment.status == "paid"), 0).label("revenue"),
        )
        .outerjoin(Payment, Payment.user_id == Referral.referred_user_id)
        .where(Referral.joined_at >= start)
        .group_by(Referral.code)
        .order_by(func.count(func.distinct(Referral.referred_user_id)).desc())
    )).all()

    codes = {row.code for row in rows} | set(metadata)
    indexed = {row.code: row for row in rows}
    items = []
    for code in sorted(codes):
        row = indexed.get(code)
        registrations = int(row.registrations if row else 0)
        buyers = int(row.buyers if row else 0)
        revenue = float(row.revenue if row else 0)
        meta = metadata.get(code, {})
        cost = float(meta.get("cost_rub", 0) or 0)
        cac = round(cost / registrations, 2) if registrations else None
        cpa = round(cost / buyers, 2) if buyers else None
        roi = round((revenue - cost) / cost * 100, 2) if cost else None
        items.append({
            "code": code,
            "title": meta.get("title") or code,
            "platform": meta.get("platform") or "",
            "note": meta.get("note") or "",
            "cost_rub": cost,
            "registrations": registrations,
            "buyers": buyers,
            "revenue": round(revenue, 2),
            "conversion": round(buyers / registrations * 100, 2) if registrations else 0,
            "cac": cac,
            "cpa": cpa,
            "roi": roi,
        })
    return {"days": days, "items": items}


@router.put("/campaigns/{code}")
async def update_campaign(
    code: str,
    body: CampaignPatch,
    admin: AdminAuth,
    settings: Settings = Depends(get_settings),
) -> dict:
    normalized = code.strip()[:64]
    payload = {**body.model_dump(), "updated_at": _now().isoformat(), "updated_by": admin}
    client = _redis(settings)
    await client.hset(CAMPAIGNS_KEY, normalized, json.dumps(payload, ensure_ascii=False))
    await client.aclose()
    await _audit(settings, admin, "campaign.updated", {"code": normalized, **body.model_dump()})
    return {"ok": True, "code": normalized, "campaign": payload}


@router.get("/users")
async def filtered_users(
    _: AdminAuth,
    session: Session,
    search: str = "",
    status: str = "all",
    source: str = "all",
    business: str = "all",
    payment: str = "all",
    active_days: int | None = Query(default=None, ge=1, le=3650),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict:
    conditions = []
    term = search.strip()
    if term:
        like = f"%{term}%"
        options = [User.username.ilike(like), User.first_name.ilike(like), User.last_name.ilike(like)]
        if term.isdigit():
            options.append(User.telegram_id == int(term))
        conditions.append(or_(*options))
    if status != "all":
        conditions.append(User.subscription_status == status)
    if active_days:
        conditions.append(User.last_seen_at >= _now().replace(tzinfo=None) - timedelta(days=active_days))
    if source == "referral":
        conditions.append(exists(select(Referral.id).where(Referral.referred_user_id == User.id)))
    elif source == "organic":
        conditions.append(~exists(select(Referral.id).where(Referral.referred_user_id == User.id)))
    if business == "connected":
        conditions.append(exists(select(BusinessConnection.id).where(BusinessConnection.owner_user_id == User.id, BusinessConnection.is_active.is_(True))))
    elif business == "disconnected":
        conditions.append(~exists(select(BusinessConnection.id).where(BusinessConnection.owner_user_id == User.id, BusinessConnection.is_active.is_(True)))
    if payment == "paid":
        conditions.append(exists(select(Payment.id).where(Payment.user_id == User.id, Payment.status == "paid")))
    elif payment == "never":
        conditions.append(~exists(select(Payment.id).where(Payment.user_id == User.id, Payment.status == "paid")))

    base = select(User).where(and_(*conditions)) if conditions else select(User)
    total = int(await session.scalar(select(func.count()).select_from(base.subquery())) or 0)
    users = list((await session.scalars(base.order_by(User.registered_at.desc()).offset(offset).limit(limit))).all())
    return {"total": total, "limit": limit, "offset": offset, "items": [serialize_user(user) for user in users]}


def _csv_response(filename: str, rows: list[list[object]]) -> StreamingResponse:
    stream = io.StringIO()
    writer = csv.writer(stream, delimiter=";")
    writer.writerows(rows)
    data = "\ufeff" + stream.getvalue()
    return StreamingResponse(iter([data.encode("utf-8")]), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/exports/users.csv")
async def export_users(admin: AdminAuth, session: Session, settings: Settings = Depends(get_settings)):
    users = list((await session.scalars(select(User).order_by(User.registered_at.desc()))).all())
    rows: list[list[object]] = [["Telegram ID", "Username", "Имя", "Статус", "Регистрация", "Последняя активность", "Trial до", "VIP до", "Заблокировал бота"]]
    for user in users:
        rows.append([
            user.telegram_id, user.username or "", " ".join(filter(None, [user.first_name, user.last_name])),
            user.subscription_status.value, user.registered_at.isoformat(), user.last_seen_at.isoformat(),
            user.trial_ends_at.isoformat(), user.vip_ends_at.isoformat() if user.vip_ends_at else "", bool(user.blocked_bot_at),
        ])
    await _audit(settings, admin, "export.users", {"count": len(users)})
    return _csv_response("phantom-users.csv", rows)


@router.get("/exports/payments.csv")
async def export_payments(admin: AdminAuth, session: Session, settings: Settings = Depends(get_settings)):
    records = (await session.execute(select(Payment, User).join(User, User.id == Payment.user_id).order_by(Payment.created_at.desc()))).all()
    rows: list[list[object]] = [["ID", "Telegram ID", "Username", "Сумма", "Валюта", "Статус", "Провайдер", "Повторный", "Создан", "Оплачен", "Возврат"]]
    for payment, user in records:
        rows.append([
            payment.id, user.telegram_id, user.username or "", str(payment.amount), payment.currency, payment.status,
            payment.provider, payment.recurring, payment.created_at.isoformat(), payment.paid_at.isoformat() if payment.paid_at else "",
            payment.refunded_at.isoformat() if payment.refunded_at else "",
        ])
    await _audit(settings, admin, "export.payments", {"count": len(records)})
    return _csv_response("phantom-payments.csv", rows)


@router.get("/audit")
async def audit_log(_: AdminAuth, settings: Settings = Depends(get_settings), limit: int = Query(100, ge=1, le=500)) -> dict:
    client = _redis(settings)
    raw = await client.lrange(AUDIT_KEY, 0, limit - 1)
    await client.aclose()
    return {"items": [json.loads(item) for item in raw]}
