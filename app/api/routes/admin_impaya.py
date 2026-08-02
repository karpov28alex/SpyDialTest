from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import String, case, cast, func, select

from app.api.routes.admin import AdminAuth, Session
from app.core.config import get_settings
from app.db.models import Payment, Subscription, SubscriptionStatus, User
from app.services.access import get_monetization_settings
from app.services.impaya import ImpayaClient, ImpayaError, successful_state

router = APIRouter(prefix="/api/admin/impaya", tags=["admin-impaya"])
settings = get_settings()


class AutoRenewPatch(BaseModel):
    enabled: bool


class TariffPatch(BaseModel):
    initial_rub: int = Field(ge=1, le=100000)
    renewal_rub: int = Field(ge=1, le=100000)
    fallback_rub: int = Field(ge=1, le=100000)


class ManualChargePrepare(BaseModel):
    user_id: int
    amount_rub: int = Field(ge=1, le=100000)
    access_days: int = Field(ge=0, le=3650)
    reason: str = Field(min_length=3, max_length=500)


class ManualChargeConfirm(BaseModel):
    confirmation_code: str = Field(min_length=6, max_length=12)


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


def _transaction(payload: dict) -> dict:
    for key in ("charge_state", "state", "binding_state"):
        block = payload.get(key)
        if isinstance(block, dict) and isinstance(block.get("transaction"), dict):
            return block["transaction"]
    response = payload.get("response")
    return response if isinstance(response, dict) else {}


def serialize_payment(payment: Payment, user: User | None, *, include_payload: bool = False) -> dict:
    payload = _payload(payment)
    transaction = _transaction(payload)
    item = {
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
        "error": payload.get("charge_error_message") or payload.get("error_message"),
        "error_code": payload.get("charge_error_code") or payload.get("error_code"),
        "reason": payload.get("reason"),
        "access_days": payload.get("access_days"),
        "source_payment_id": payload.get("source_payment_id"),
        "prepared_by": payload.get("prepared_by"),
        "confirmed_by": payload.get("confirmed_by"),
        "created_at": _iso(payment.created_at),
        "paid_at": _iso(payment.paid_at),
    }
    if include_payload:
        item["payload"] = payload
    return item


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


async def _binding_for_user(session: Session, user_id: int) -> tuple[Payment, User] | None:
    for payment, user in await _binding_sources(session):
        if user.id == user_id:
            return payment, user
    return None


@router.get("/overview")
async def overview(_: AdminAuth, session: Session) -> dict:
    now = datetime.now(UTC)
    start_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_7d = now - timedelta(days=7)
    start_30d = now - timedelta(days=30)
    base = Payment.provider == "impaya"
    paid = Payment.status == "paid"
    stats = (await session.execute(select(
        func.count(Payment.id).filter(base).label("total"),
        func.count(Payment.id).filter(base, paid).label("paid"),
        func.count(Payment.id).filter(base, Payment.status == "failed").label("failed"),
        func.count(Payment.id).filter(base, Payment.status.in_(["pending", "processing"])).label("pending"),
        func.coalesce(func.sum(case((base & paid, Payment.amount), else_=0)), 0).label("revenue"),
        func.coalesce(func.sum(case((base & paid & (Payment.paid_at >= start_today), Payment.amount), else_=0)), 0).label("today"),
        func.coalesce(func.sum(case((base & paid & (Payment.paid_at >= start_7d), Payment.amount), else_=0)), 0).label("last_7d"),
        func.coalesce(func.sum(case((base & paid & (Payment.paid_at >= start_30d), Payment.amount), else_=0)), 0).label("last_30d"),
    ))).one()
    sources = await _binding_sources(session)
    active_auto = sum(1 for payment, _ in sources if _payload(payment).get("auto_renew") is not False)
    upcoming = sum(1 for payment, user in sources if _payload(payment).get("auto_renew") is not False and user.vip_ends_at and user.vip_ends_at > now)
    config = await get_monetization_settings(session)
    total_count = int(stats.total or 0)
    paid_count = int(stats.paid or 0)
    failed_count = int(stats.failed or 0)
    return {
        "payments": {
            "total": total_count,
            "paid": paid_count,
            "failed": failed_count,
            "pending": int(stats.pending or 0),
            "success_rate": round(paid_count / total_count * 100, 1) if total_count else 0,
            "failure_rate": round(failed_count / total_count * 100, 1) if total_count else 0,
        },
        "revenue": {
            "all_time": _money(stats.revenue),
            "today": _money(stats.today),
            "last_7d": _money(stats.last_7d),
            "last_30d": _money(stats.last_30d),
        },
        "subscriptions": {
            "cards_bound": len(sources),
            "auto_renew_enabled": active_auto,
            "auto_renew_disabled": max(len(sources) - active_auto, 0),
            "upcoming_charges": upcoming,
        },
        "tariffs": {
            "initial_rub": config.entry_price_rub,
            "renewal_rub": config.weekly_price_rub,
            "fallback_rub": config.fallback_three_day_price_rub,
        },
    }


@router.patch("/tariffs")
async def patch_tariffs(body: TariffPatch, admin: AdminAuth, session: Session) -> dict:
    row = await get_monetization_settings(session)
    row.entry_price_rub = body.initial_rub
    row.weekly_price_rub = body.renewal_rub
    row.fallback_three_day_price_rub = body.fallback_rub
    row.updated_by = admin
    row.updated_at = datetime.now(UTC)
    await session.commit()
    return {"ok": True, "tariffs": {"initial_rub": row.entry_price_rub, "renewal_rub": row.weekly_price_rub, "fallback_rub": row.fallback_three_day_price_rub}}


@router.get("/payments")
async def payments(
    _: AdminAuth,
    session: Session,
    status: str = Query("all", pattern="^(all|paid|failed|pending|processing)$"),
    kind: str = Query("all", max_length=40),
    q: str = Query("", max_length=160),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    conditions = [Payment.provider == "impaya"]
    if status != "all":
        conditions.append(Payment.status == status)
    if q.strip():
        term = f"%{q.strip()}%"
        conditions.append(Payment.external_id.ilike(term) | User.username.ilike(term) | cast(User.telegram_id, String).ilike(term))
    query = select(Payment, User).join(User, User.id == Payment.user_id).where(*conditions)
    count_query = select(func.count(Payment.id)).join(User, User.id == Payment.user_id).where(*conditions)
    total = int(await session.scalar(count_query) or 0)
    rows = list((await session.execute(query.order_by(Payment.id.desc()).offset(offset).limit(limit))).all())
    items = [serialize_payment(payment, user) for payment, user in rows]
    if kind != "all":
        items = [item for item in items if item["kind"] == kind]
    return {"items": items, "count": len(items), "total": total, "limit": limit, "offset": offset}


@router.get("/payments/{payment_id}")
async def payment_details(payment_id: int, _: AdminAuth, session: Session) -> dict:
    row = (await session.execute(
        select(Payment, User).join(User, User.id == Payment.user_id).where(Payment.id == payment_id, Payment.provider == "impaya")
    )).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Операция не найдена")
    payment, user = row
    return serialize_payment(payment, user, include_payload=True)


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
    items.sort(key=lambda item: (item["next_charge_at"] is None, item["next_charge_at"] or "", item["telegram_id"]))
    return {"items": items, "count": len(items)}


@router.patch("/subscriptions/{user_id}/auto-renew")
async def patch_auto_renew(user_id: int, body: AutoRenewPatch, _: AdminAuth, session: Session) -> dict:
    source = await _binding_for_user(session, user_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Рекуррентная привязка не найдена")
    payment, user = source
    payload = _payload(payment)
    payment.payload = {**payload, "auto_renew": body.enabled, "auto_renew_changed_at": datetime.now(UTC).isoformat(), "auto_renew_changed_by": "admin"}
    await session.commit()
    return {"ok": True, "user_id": user.id, "telegram_id": user.telegram_id, "auto_renew": body.enabled, "vip_ends_at": _iso(user.vip_ends_at)}


@router.post("/manual-charge/prepare")
async def prepare_manual_charge(body: ManualChargePrepare, admin: AdminAuth, session: Session) -> dict:
    source = await _binding_for_user(session, body.user_id)
    if source is None:
        raise HTTPException(status_code=404, detail="У пользователя нет активной привязки карты")
    source_payment, user = source
    code = f"{secrets.randbelow(900000) + 100000}"
    operation_id = f"ph_manual_{user.id}_{secrets.token_hex(8)}"
    payment = Payment(
        user_id=user.id,
        provider="impaya",
        external_id=operation_id,
        amount=Decimal(body.amount_rub),
        currency="RUB",
        status="pending",
        recurring=True,
        payload={
            "kind": "manual",
            "source_payment_id": source_payment.id,
            "amount_rub": body.amount_rub,
            "access_days": body.access_days,
            "reason": body.reason,
            "prepared_by": admin,
            "confirmation_code": code,
            "confirmation_expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        },
    )
    session.add(payment)
    await session.commit()
    await session.refresh(payment)
    card = _card(_payload(source_payment))
    return {
        "request_id": payment.id,
        "operation_id": operation_id,
        "confirmation_code": code,
        "expires_in_seconds": 600,
        "user": {"id": user.id, "telegram_id": user.telegram_id, "username": user.username, "name": " ".join(filter(None, [user.first_name, user.last_name]))},
        "card": {"pan_mask": card.get("pan_mask"), "bank_name": card.get("bank_name")},
        "amount_rub": body.amount_rub,
        "access_days": body.access_days,
        "reason": body.reason,
    }


@router.post("/manual-charge/{request_id}/confirm")
async def confirm_manual_charge(request_id: int, body: ManualChargeConfirm, admin: AdminAuth, session: Session) -> dict:
    payment = await session.scalar(select(Payment).where(Payment.id == request_id, Payment.provider == "impaya").with_for_update())
    if payment is None or _kind(_payload(payment)) != "manual":
        raise HTTPException(status_code=404, detail="Заявка не найдена")
    payload = _payload(payment)
    if payment.status != "pending":
        raise HTTPException(status_code=409, detail="Заявка уже обработана")
    expires_raw = payload.get("confirmation_expires_at")
    if not expires_raw:
        raise HTTPException(status_code=409, detail="У заявки отсутствует срок подтверждения")
    expires = datetime.fromisoformat(str(expires_raw))
    if expires < datetime.now(UTC):
        payment.status = "failed"
        payment.payload = {**payload, "error_message": "confirmation expired"}
        await session.commit()
        raise HTTPException(status_code=409, detail="Код подтверждения истёк")
    if not secrets.compare_digest(str(payload.get("confirmation_code")), body.confirmation_code.strip()):
        raise HTTPException(status_code=403, detail="Неверный код подтверждения")
    source = await _binding_for_user(session, payment.user_id)
    if source is None:
        raise HTTPException(status_code=409, detail="Привязка карты больше недоступна")
    source_payment, user = source
    source_payload = _payload(source_payment)
    client = ImpayaClient(settings)
    try:
        response = await client.recurrent_pay(
            customer_operation_id=payment.external_id,
            amount_rub=int(payment.amount),
            binding_id=str(source_payload["binding_id"]),
            impaya_user_id=str(source_payload["impaya_user_id"]),
            merchant_user_id=str(source_payload.get("merchant_user_id") or ""),
            description=f"Ручное списание Phantom: {payload.get('reason')}",
        )
        state = await client.transaction_state(customer_operation_id=payment.external_id, extended=True, terminal_name=settings.impaya_non3ds_terminal_name)
    except ImpayaError as exc:
        payment.status = "failed"
        payment.payload = {**payload, "confirmation_code": None, "confirmed_by": admin, "confirmed_at": datetime.now(UTC).isoformat(), "error_code": exc.code, "error_message": str(exc), "error_response": exc.payload}
        await session.commit()
        raise HTTPException(status_code=502, detail=f"Impaya: {exc}") from exc
    payment.payload = {**payload, "confirmation_code": None, "confirmed_by": admin, "confirmed_at": datetime.now(UTC).isoformat(), "response": response, "state": state}
    if not successful_state(state):
        payment.status = "failed"
        await session.commit()
        raise HTTPException(status_code=409, detail="Impaya не подтвердила списание")
    now = datetime.now(UTC)
    days = int(payload.get("access_days") or 0)
    if days > 0:
        current_end = user.vip_ends_at if user.vip_ends_at and user.vip_ends_at > now else now
        user.vip_ends_at = current_end + timedelta(days=days)
        user.subscription_status = SubscriptionStatus.vip
        session.add(Subscription(user_id=user.id, status="active", source="impaya_manual", starts_at=now, ends_at=user.vip_ends_at))
    payment.status = "paid"
    payment.paid_at = now
    payment.payload = {**payment.payload, "new_vip_ends_at": _iso(user.vip_ends_at)}
    await session.commit()
    return {"ok": True, "payment_id": payment.id, "status": payment.status, "amount_rub": _money(payment.amount), "vip_ends_at": _iso(user.vip_ends_at)}
