from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import case, func, select

from app.api.routes.admin import AdminAuth, Session
from app.db.models import Payment, User
from app.services.access import get_monetization_settings

router = APIRouter(prefix="/api/admin/impaya", tags=["admin-impaya"])


class AutoRenewPatch(BaseModel):
    enabled: bool


def _payload(payment: Payment) -> dict:
    return payment.payload if isinstance(payment.payload, dict) else {}


def _card(payload: dict) -> dict:
    direct = payload.get("card")
    if isinstance(direct, dict) and direct:
        return direct
    binding_state = payload.get("binding_state")
    if isinstance(binding_state, dict):
        option = binding_state.get("payment_option")
        if isinstance(option, dict) and isinstance(option.get("card"), dict):
            return option["card"]
    return {}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _money(value: Decimal | int | float | None) -> float:
    return float(value or 0)


def _kind(payload: dict) -> str:
    return str(payload.get("kind") or "unknown")


def serialize_payment(payment: Payment, user: User | None) -> dict:
    payload = _payload(payment)
    transaction = payload.get("charge_state", {}).get("transaction", {}) if isinstance(payload.get("charge_state"), dict) else {}
    if not transaction and isinstance(payload.get("state"), dict):
        transaction = payload["state"].get("transaction", {}) or {}
    error = payload.get("charge_error_message") or payload.get("error_message")
    return {
        "id": payment.id,
        "external_id": payment.external_id,
        "user_id": payment.user_id,
        "telegram_id": user.telegram_id if user else None,
        "username": user.username if user else None,
        "name": " ".join(filter(None, [user.first_name, user.last_name])) if user else "",
        "amount": _money(payment.amount),
        "currency": payment.currency,
        "status": payment.status,
        "recurring": payment.recurring,
        "kind": _kind(payload),
        "transaction_id": transaction.get("transaction_id") or payload.get("transaction_id"),
        "terminal_name": transaction.get("terminal_name"),
        "state": transaction.get("state"),
        "error": error,
        "created_at": _iso(payment.created_at),
        "paid_at": _iso(payment.paid_at),
    }


async def _binding_sources(session: Session) -> list[tuple[Payment, User]]:
    rows = list((await session.execute(
        select(Payment, User)
        .join(User, User.id == Payment.user_id)
        .where(Payment.provider == "impaya", Payment.recurring.is_(True))
        .order_by(Payment.id.desc())
    )).all())
    seen: set[int] = set()
    result: list[tuple[Payment, User]] = []
    for payment, user in rows:
        if user.id in seen:
            continue
        payload = _payload(payment)
        if payload.get("binding_id") and payload.get("impaya_user_id"):
            seen.add(user.id)
            result.append((payment, user))
    return result


@router.get("/overview")
async def overview(_: AdminAuth, session: Session) -> dict:
    now = datetime.now(UTC)
    start_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_30d = now - timedelta(days=30)

    base = Payment.provider == "impaya"
    paid = Payment.status == "paid"
    stats = (await session.execute(
        select(
            func.count(Payment.id).filter(base).label("total"),
            func.count(Payment.id).filter(base, paid).label("paid"),
            func.count(Payment.id).filter(base, Payment.status == "failed").label("failed"),
            func.count(Payment.id).filter(base, Payment.status == "pending").label("pending"),
            func.coalesce(func.sum(case((base & paid, Payment.amount), else_=0)), 0).label("revenue"),
            func.coalesce(func.sum(case((base & paid & (Payment.paid_at >= start_today), Payment.amount), else_=0)), 0).label("today"),
            func.coalesce(func.sum(case((base & paid & (Payment.paid_at >= start_30d), Payment.amount), else_=0)), 0).label("last_30d"),
        )
    )).one()

    sources = await _binding_sources(session)
    active_auto = 0
    cards = 0
    upcoming = 0
    for payment, user in sources:
        payload = _payload(payment)
        cards += 1
        if payload.get("auto_renew") is not False:
            active_auto += 1
            if user.vip_ends_at and user.vip_ends_at > now:
                upcoming += 1

    paid_count = int(stats.paid or 0)
    total_count = int(stats.total or 0)
    settings = await get_monetization_settings(session)
    return {
        "payments": {
            "total": total_count,
            "paid": paid_count,
            "failed": int(stats.failed or 0),
            "pending": int(stats.pending or 0),
            "success_rate": round((paid_count / total_count * 100), 1) if total_count else 0,
        },
        "revenue": {
            "all_time": _money(stats.revenue),
            "today": _money(stats.today),
            "last_30d": _money(stats.last_30d),
        },
        "subscriptions": {
            "cards_bound": cards,
            "auto_renew_enabled": active_auto,
            "upcoming_charges": upcoming,
        },
        "tariffs": {
            "initial_rub": settings.entry_price_rub,
            "renewal_rub": settings.weekly_price_rub,
            "fallback_rub": settings.fallback_three_day_price_rub,
        },
    }


@router.get("/payments")
async def payments(
    _: AdminAuth,
    session: Session,
    status: str = Query("all", pattern="^(all|paid|failed|pending)$"),
    kind: str = Query("all", max_length=40),
    q: str = Query("", max_length=160),
    limit: int = Query(100, ge=1, le=500),
) -> dict:
    query = (
        select(Payment, User)
        .join(User, User.id == Payment.user_id)
        .where(Payment.provider == "impaya")
    )
    if status != "all":
        query = query.where(Payment.status == status)
    if q.strip():
        term = f"%{q.strip()}%"
        query = query.where(
            (Payment.external_id.ilike(term))
            | (User.username.ilike(term))
            | (func.cast(User.telegram_id, str).ilike(term))
        )
    rows = list((await session.execute(query.order_by(Payment.id.desc()).limit(limit))).all())
    items = [serialize_payment(payment, user) for payment, user in rows]
    if kind != "all":
        items = [item for item in items if item["kind"] == kind]
    return {"items": items, "count": len(items)}


@router.get("/subscriptions")
async def subscriptions(_: AdminAuth, session: Session) -> dict:
    now = datetime.now(UTC)
    items = []
    for payment, user in await _binding_sources(session):
        payload = _payload(payment)
        card = _card(payload)
        auto_renew = payload.get("auto_renew") is not False
        items.append({
            "user_id": user.id,
            "telegram_id": user.telegram_id,
            "username": user.username,
            "name": " ".join(filter(None, [user.first_name, user.last_name])),
            "active": bool(user.vip_ends_at and user.vip_ends_at > now),
            "vip_ends_at": _iso(user.vip_ends_at),
            "next_charge_at": _iso(user.vip_ends_at) if auto_renew and user.vip_ends_at and user.vip_ends_at > now else None,
            "auto_renew": auto_renew,
            "source_payment_id": payment.id,
            "binding_id": payload.get("binding_id"),
            "pan_mask": card.get("pan_mask"),
            "bank_name": card.get("bank_name"),
            "card_type": card.get("card_type"),
        })
    return {"items": items, "count": len(items)}


@router.patch("/subscriptions/{user_id}/auto-renew")
async def patch_auto_renew(user_id: int, body: AutoRenewPatch, _: AdminAuth, session: Session) -> dict:
    sources = await _binding_sources(session)
    source = next(((payment, user) for payment, user in sources if user.id == user_id), None)
    if source is None:
        raise HTTPException(status_code=404, detail="Рекуррентная привязка не найдена")
    payment, user = source
    payload = _payload(payment)
    payment.payload = {
        **payload,
        "auto_renew": body.enabled,
        "auto_renew_changed_at": datetime.now(UTC).isoformat(),
        "auto_renew_changed_by": "admin",
    }
    await session.commit()
    return {
        "ok": True,
        "user_id": user.id,
        "telegram_id": user.telegram_id,
        "auto_renew": body.enabled,
        "vip_ends_at": _iso(user.vip_ends_at),
    }
