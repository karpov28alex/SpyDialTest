from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Query
from sqlalchemy import case, desc, func, select

from app.api.routes.admin import AdminAuth, Session
from app.db.models import (
    BusinessConnection,
    FailedUpdate,
    Job,
    Payment,
    Referral,
    Subscription,
    User,
)

router = APIRouter(prefix="/api/admin/analytics", tags=["admin-analytics"])


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _money(value: Decimal | int | float | None) -> float:
    return round(float(value or 0), 2)


def _period(days: int) -> tuple[datetime, datetime]:
    end = _now()
    return end - timedelta(days=days - 1), end


@router.get("/finance")
async def finance(
    _: AdminAuth,
    session: Session,
    days: int = Query(30, ge=1, le=366),
) -> dict:
    start, end = _period(days)
    paid_condition = Payment.status == "paid"
    payment_date = func.coalesce(Payment.paid_at, Payment.created_at)

    revenue = await session.scalar(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(
            paid_condition,
            payment_date >= start,
            payment_date <= end,
        )
    )
    revenue_total = await session.scalar(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(paid_condition)
    )
    paid_count = await session.scalar(
        select(func.count(Payment.id)).where(
            paid_condition,
            payment_date >= start,
            payment_date <= end,
        )
    )
    failed_count = await session.scalar(
        select(func.count(Payment.id)).where(
            Payment.status.in_(["failed", "declined", "cancelled"]),
            Payment.created_at >= start,
        )
    )
    refunds = await session.scalar(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.refunded_at.is_not(None),
            Payment.refunded_at >= start,
        )
    )
    paying_users = await session.scalar(
        select(func.count(func.distinct(Payment.user_id))).where(
            paid_condition,
            payment_date >= start,
        )
    )
    users_total = await session.scalar(select(func.count(User.id)))
    active_subscriptions = await session.scalar(
        select(func.count(Subscription.id)).where(
            Subscription.status.in_(["active", "paid", "vip"]),
            Subscription.ends_at > _now(),
        )
    )
    recurring_count = await session.scalar(
        select(func.count(Payment.id)).where(
            paid_condition,
            Payment.recurring.is_(True),
            payment_date >= start,
        )
    )

    daily_rows = (
        await session.execute(
            select(
                func.date(payment_date).label("day"),
                func.coalesce(func.sum(Payment.amount).filter(paid_condition), 0).label("revenue"),
                func.count(Payment.id).filter(paid_condition).label("paid"),
                func.count(Payment.id).filter(Payment.status.in_(["failed", "declined", "cancelled"])).label("failed"),
            )
            .where(payment_date >= start)
            .group_by(func.date(payment_date))
            .order_by(func.date(payment_date))
        )
    ).all()

    status_rows = (
        await session.execute(
            select(Payment.status, func.count(Payment.id), func.coalesce(func.sum(Payment.amount), 0))
            .where(Payment.created_at >= start)
            .group_by(Payment.status)
            .order_by(desc(func.count(Payment.id)))
        )
    ).all()

    transaction_rows = (
        await session.execute(
            select(Payment, User)
            .join(User, User.id == Payment.user_id)
            .order_by(desc(Payment.created_at))
            .limit(80)
        )
    ).all()

    revenue_value = _money(revenue)
    paying_value = int(paying_users or 0)
    total_users_value = int(users_total or 0)
    paid_value = int(paid_count or 0)
    return {
        "period": {"days": days, "from": start.isoformat(), "to": end.isoformat()},
        "metrics": {
            "revenue": revenue_value,
            "revenue_total": _money(revenue_total),
            "paid_payments": paid_value,
            "failed_payments": int(failed_count or 0),
            "refunds": _money(refunds),
            "paying_users": paying_value,
            "active_subscriptions": int(active_subscriptions or 0),
            "recurring_payments": int(recurring_count or 0),
            "arppu": round(revenue_value / paying_value, 2) if paying_value else 0,
            "average_payment": round(revenue_value / paid_value, 2) if paid_value else 0,
            "conversion": round(paying_value / total_users_value * 100, 2) if total_users_value else 0,
        },
        "daily": [
            {"date": str(day), "revenue": _money(day_revenue), "paid": int(paid), "failed": int(failed)}
            for day, day_revenue, paid, failed in daily_rows
        ],
        "statuses": [
            {"status": status, "count": int(count), "amount": _money(amount)}
            for status, count, amount in status_rows
        ],
        "transactions": [
            {
                "id": payment.id,
                "user_id": user.id,
                "telegram_id": user.telegram_id,
                "username": user.username,
                "name": " ".join(filter(None, [user.first_name, user.last_name])),
                "amount": _money(payment.amount),
                "currency": payment.currency,
                "status": payment.status,
                "provider": payment.provider,
                "recurring": payment.recurring,
                "external_id": payment.external_id,
                "created_at": payment.created_at.isoformat(),
                "paid_at": payment.paid_at.isoformat() if payment.paid_at else None,
                "refunded_at": payment.refunded_at.isoformat() if payment.refunded_at else None,
                "plan": (payment.payload or {}).get("plan") or (payment.payload or {}).get("tariff"),
                "error": (payment.payload or {}).get("error") or (payment.payload or {}).get("failure_reason"),
            }
            for payment, user in transaction_rows
        ],
    }


@router.get("/acquisition")
async def acquisition(
    _: AdminAuth,
    session: Session,
    days: int = Query(30, ge=1, le=366),
) -> dict:
    start, _ = _period(days)
    source_expr = case(
        (User.referrer_user_id.is_not(None), "referral"),
        else_="organic",
    )
    source_rows = (
        await session.execute(
            select(source_expr.label("source"), func.count(User.id))
            .where(User.registered_at >= start)
            .group_by(source_expr)
        )
    ).all()

    referred_users = (
        await session.execute(
            select(
                Referral.code,
                func.count(Referral.referred_user_id).label("registrations"),
                func.count(func.distinct(Payment.user_id)).filter(Payment.status == "paid").label("buyers"),
                func.coalesce(func.sum(Payment.amount).filter(Payment.status == "paid"), 0).label("revenue"),
            )
            .outerjoin(Payment, Payment.user_id == Referral.referred_user_id)
            .where(Referral.joined_at >= start)
            .group_by(Referral.code)
            .order_by(desc(func.count(Referral.referred_user_id)))
            .limit(100)
        )
    ).all()

    funnel = {
        "started": int(await session.scalar(select(func.count(User.id)).where(User.registered_at >= start)) or 0),
        "business_connected": int(
            await session.scalar(
                select(func.count(func.distinct(BusinessConnection.owner_user_id))).where(
                    BusinessConnection.connected_at >= start
                )
            )
            or 0
        ),
        "referred": int(
            await session.scalar(select(func.count(Referral.id)).where(Referral.joined_at >= start)) or 0
        ),
        "paid": int(
            await session.scalar(
                select(func.count(func.distinct(Payment.user_id))).where(
                    Payment.status == "paid",
                    func.coalesce(Payment.paid_at, Payment.created_at) >= start,
                )
            )
            or 0
        ),
    }
    return {
        "period": {"days": days, "from": start.isoformat()},
        "sources": [{"source": source, "count": int(count)} for source, count in source_rows],
        "funnel": funnel,
        "campaigns": [
            {
                "code": code,
                "registrations": int(registrations),
                "buyers": int(buyers),
                "revenue": _money(revenue),
                "conversion": round(int(buyers) / int(registrations) * 100, 2) if registrations else 0,
            }
            for code, registrations, buyers, revenue in referred_users
        ],
    }


@router.get("/operations")
async def operations(
    _: AdminAuth,
    session: Session,
    limit: int = Query(100, ge=10, le=300),
) -> dict:
    failed_rows = list(
        (
            await session.scalars(
                select(FailedUpdate).order_by(desc(FailedUpdate.created_at)).limit(limit // 2)
            )
        ).all()
    )
    job_rows = list(
        (
            await session.scalars(
                select(Job).order_by(desc(Job.created_at)).limit(limit // 2)
            )
        ).all()
    )
    events: list[dict] = []
    for row in failed_rows:
        events.append(
            {
                "type": "error",
                "title": row.update_type,
                "status": "resolved" if row.resolved else "open",
                "details": row.error,
                "at": row.created_at.isoformat(),
                "correlation_id": row.correlation_id,
            }
        )
    for row in job_rows:
        events.append(
            {
                "type": "job",
                "title": row.kind,
                "status": row.status,
                "details": row.last_error,
                "at": row.created_at.isoformat(),
                "attempts": row.attempts,
            }
        )
    events.sort(key=lambda item: item["at"], reverse=True)

    status_rows = (
        await session.execute(select(Job.status, func.count(Job.id)).group_by(Job.status))
    ).all()
    return {
        "queue": {status: int(count) for status, count in status_rows},
        "unresolved_errors": int(
            await session.scalar(select(func.count(FailedUpdate.id)).where(FailedUpdate.resolved.is_(False))) or 0
        ),
        "events": events[:limit],
    }
