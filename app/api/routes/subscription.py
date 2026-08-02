from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from app.api.deps import CurrentUser, SessionDep
from app.db.models import Payment

router = APIRouter(prefix="/api/subscription", tags=["subscription"])


async def _binding_payment(session: SessionDep, user_id: int) -> Payment | None:
    payments = list((await session.scalars(
        select(Payment)
        .where(
            Payment.user_id == user_id,
            Payment.provider == "impaya",
            Payment.recurring.is_(True),
        )
        .order_by(Payment.id.desc())
    )).all())
    for payment in payments:
        payload = payment.payload if isinstance(payment.payload, dict) else {}
        if payload.get("binding_id") and payload.get("impaya_user_id"):
            return payment
    return None


def _serialize(user: CurrentUser, payment: Payment | None) -> dict:
    now = datetime.now(UTC)
    payload = payment.payload if payment and isinstance(payment.payload, dict) else {}
    card = payload.get("card") if isinstance(payload.get("card"), dict) else {}
    active = bool(user.vip_ends_at and user.vip_ends_at > now)
    auto_renew = bool(payment and payload.get("auto_renew") is not False)
    return {
        "active": active,
        "status": str(user.subscription_status.value if hasattr(user.subscription_status, "value") else user.subscription_status),
        "vip_ends_at": user.vip_ends_at.isoformat() if user.vip_ends_at else None,
        "auto_renew": auto_renew,
        "has_binding": payment is not None,
        "next_charge_at": user.vip_ends_at.isoformat() if active and auto_renew else None,
        "card": {
            "pan_mask": card.get("pan_mask"),
            "bank_name": card.get("bank_name"),
            "card_type": card.get("card_type"),
            "exp_month": card.get("exp_month"),
            "exp_year": card.get("exp_year"),
        } if payment else None,
    }


@router.get("")
async def subscription_status(user: CurrentUser, session: SessionDep) -> dict:
    payment = await _binding_payment(session, user.id)
    return _serialize(user, payment)


@router.post("/cancel")
async def cancel_subscription(user: CurrentUser, session: SessionDep) -> dict:
    payment = await _binding_payment(session, user.id)
    if payment is None:
        raise HTTPException(status_code=404, detail="Актуальной подписки не найдено.")
    payload = payment.payload if isinstance(payment.payload, dict) else {}
    if payload.get("auto_renew") is False:
        return _serialize(user, payment)
    payment.payload = {
        **payload,
        "auto_renew": False,
        "auto_renew_cancelled_at": datetime.now(UTC).isoformat(),
    }
    await session.commit()
    return _serialize(user, payment)
