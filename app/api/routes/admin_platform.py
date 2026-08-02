from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Query
from sqlalchemy import case, desc, func, select

from app.api.routes.admin import AdminAuth, Session
from app.db.models import BusinessConnection, FailedUpdate, Job, Payment, Referral, User

router = APIRouter(prefix="/api/admin/platform", tags=["admin-platform"])


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _money(value: Decimal | int | float | None) -> float:
    return round(float(value or 0), 2)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


@router.get("/dashboard")
async def dashboard(
    _: AdminAuth,
    session: Session,
    days: int = Query(30, ge=1, le=366),
) -> dict:
    now = _now()
    start = now - timedelta(days=days)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    seven_days = now - timedelta(days=7)
    thirty_days = now - timedelta(days=30)
    active_cutoff = now - timedelta(minutes=15)
    payment_date = func.coalesce(Payment.paid_at, Payment.created_at)
    paid = Payment.status == "paid"
    failed_statuses = ["failed", "declined", "cancelled", "rejected"]

    users_total = int(await session.scalar(select(func.count(User.id))) or 0)
    users_today = int(
        await session.scalar(select(func.count(User.id)).where(User.registered_at >= today)) or 0
    )
    users_period = int(
        await session.scalar(select(func.count(User.id)).where(User.registered_at >= start)) or 0
    )
    online_now = int(
        await session.scalar(select(func.count(User.id)).where(User.last_seen_at >= active_cutoff)) or 0
    )
    blocked = int(
        await session.scalar(select(func.count(User.id)).where(User.blocked_bot_at.is_not(None))) or 0
    )

    business_active = int(
        await session.scalar(
            select(func.count(func.distinct(BusinessConnection.owner_user_id))).where(
                BusinessConnection.is_active.is_(True)
            )
        )
        or 0
    )
    business_period = int(
        await session.scalar(
            select(func.count(func.distinct(BusinessConnection.owner_user_id))).where(
                BusinessConnection.is_active.is_(True),
                BusinessConnection.connected_at >= start,
            )
        )
        or 0
    )

    trial_active = int(
        await session.scalar(
            select(func.count(User.id)).where(
                User.subscription_status.in_(["trial", "referral"]),
                User.trial_ends_at > now,
                User.is_access_disabled.is_(False),
            )
        )
        or 0
    )
    vip_active = int(
        await session.scalar(
            select(func.count(User.id)).where(
                User.vip_ends_at > now,
                User.is_access_disabled.is_(False),
            )
        )
        or 0
    )
    expired = int(
        await session.scalar(
            select(func.count(User.id)).where(
                User.trial_ends_at <= now,
                (User.vip_ends_at.is_(None) | (User.vip_ends_at <= now)),
                User.is_access_disabled.is_(False),
            )
        )
        or 0
    )

    revenue_total = _money(
        await session.scalar(select(func.coalesce(func.sum(Payment.amount), 0)).where(paid))
    )
    revenue_today = _money(
        await session.scalar(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(paid, payment_date >= today)
        )
    )
    revenue_7d = _money(
        await session.scalar(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(paid, payment_date >= seven_days)
        )
    )
    revenue_30d = _money(
        await session.scalar(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(paid, payment_date >= thirty_days)
        )
    )
    paid_period = int(
        await session.scalar(select(func.count(Payment.id)).where(paid, payment_date >= start)) or 0
    )
    failed_period = int(
        await session.scalar(
            select(func.count(Payment.id)).where(
                Payment.status.in_(failed_statuses), Payment.created_at >= start
            )
        )
        or 0
    )
    paying_users = int(
        await session.scalar(
            select(func.count(func.distinct(Payment.user_id))).where(paid, payment_date >= start)
        )
        or 0
    )
    recurring_paid = int(
        await session.scalar(
            select(func.count(Payment.id)).where(
                paid, Payment.recurring.is_(True), payment_date >= start
            )
        )
        or 0
    )
    active_bindings = int(
        await session.scalar(
            select(func.count(func.distinct(Payment.user_id))).where(
                Payment.provider == "impaya",
                Payment.recurring.is_(True),
                Payment.status == "paid",
            )
        )
        or 0
    )

    referrals_period = int(
        await session.scalar(select(func.count(Referral.id)).where(Referral.joined_at >= start)) or 0
    )
    unresolved_errors = int(
        await session.scalar(select(func.count(FailedUpdate.id)).where(FailedUpdate.resolved.is_(False)))
        or 0
    )
    failed_jobs = int(
        await session.scalar(select(func.count(Job.id)).where(Job.status == "failed")) or 0
    )
    queued_jobs = int(
        await session.scalar(select(func.count(Job.id)).where(Job.status.in_(["queued", "retry"])))
        or 0
    )

    daily_rows = (
        await session.execute(
            select(
                func.date(payment_date).label("day"),
                func.coalesce(func.sum(Payment.amount).filter(paid), 0).label("revenue"),
                func.count(Payment.id).filter(paid).label("paid_count"),
                func.count(Payment.id).filter(Payment.status.in_(failed_statuses)).label("failed_count"),
            )
            .where(payment_date >= start)
            .group_by(func.date(payment_date))
            .order_by(func.date(payment_date))
        )
    ).all()

    registration_rows = (
        await session.execute(
            select(func.date(User.registered_at), func.count(User.id))
            .where(User.registered_at >= start)
            .group_by(func.date(User.registered_at))
            .order_by(func.date(User.registered_at))
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

    payment_rows = (
        await session.execute(
            select(Payment, User)
            .join(User, User.id == Payment.user_id)
            .order_by(desc(Payment.created_at))
            .limit(12)
        )
    ).all()
    user_rows = list(
        (
            await session.scalars(select(User).order_by(desc(User.registered_at)).limit(12))
        ).all()
    )
    error_rows = list(
        (
            await session.scalars(
                select(FailedUpdate).order_by(desc(FailedUpdate.created_at)).limit(8)
            )
        ).all()
    )

    started = users_period
    business_conversion = round(business_period / started * 100, 2) if started else 0
    paid_conversion = round(paying_users / started * 100, 2) if started else 0
    success_rate = round(paid_period / (paid_period + failed_period) * 100, 2) if paid_period + failed_period else 0
    arppu = round(revenue_30d / paying_users, 2) if paying_users else 0
    average_payment = round(revenue_30d / paid_period, 2) if paid_period else 0

    return {
        "generated_at": now.isoformat(),
        "period": {"days": days, "from": start.isoformat(), "to": now.isoformat()},
        "users": {
            "total": users_total,
            "today": users_today,
            "period": users_period,
            "online_now": online_now,
            "blocked": blocked,
            "trial_active": trial_active,
            "vip_active": vip_active,
            "expired": expired,
        },
        "business": {"active": business_active, "period": business_period},
        "finance": {
            "revenue_total": revenue_total,
            "revenue_today": revenue_today,
            "revenue_7d": revenue_7d,
            "revenue_30d": revenue_30d,
            "paid_period": paid_period,
            "failed_period": failed_period,
            "paying_users": paying_users,
            "recurring_paid": recurring_paid,
            "active_bindings": active_bindings,
            "success_rate": success_rate,
            "arppu": arppu,
            "average_payment": average_payment,
        },
        "growth": {
            "referrals": referrals_period,
            "business_conversion": business_conversion,
            "paid_conversion": paid_conversion,
        },
        "operations": {
            "unresolved_errors": unresolved_errors,
            "failed_jobs": failed_jobs,
            "queued_jobs": queued_jobs,
        },
        "charts": {
            "payments": [
                {
                    "date": str(day),
                    "revenue": _money(revenue),
                    "paid": int(paid_count),
                    "failed": int(failed_count),
                }
                for day, revenue, paid_count, failed_count in daily_rows
            ],
            "registrations": [
                {"date": str(day), "count": int(count)} for day, count in registration_rows
            ],
            "payment_statuses": [
                {"status": status, "count": int(count), "amount": _money(amount)}
                for status, count, amount in status_rows
            ],
        },
        "recent": {
            "payments": [
                {
                    "id": payment.id,
                    "telegram_id": user.telegram_id,
                    "username": user.username,
                    "name": " ".join(filter(None, [user.first_name, user.last_name])),
                    "amount": _money(payment.amount),
                    "currency": payment.currency,
                    "status": payment.status,
                    "provider": payment.provider,
                    "recurring": payment.recurring,
                    "external_id": payment.external_id,
                    "created_at": _iso(payment.created_at),
                }
                for payment, user in payment_rows
            ],
            "users": [
                {
                    "id": user.id,
                    "telegram_id": user.telegram_id,
                    "username": user.username,
                    "name": " ".join(filter(None, [user.first_name, user.last_name])),
                    "status": str(user.subscription_status.value if hasattr(user.subscription_status, "value") else user.subscription_status),
                    "registered_at": _iso(user.registered_at),
                    "last_seen_at": _iso(user.last_seen_at),
                }
                for user in user_rows
            ],
            "errors": [
                {
                    "id": row.id,
                    "type": row.update_type,
                    "message": row.error,
                    "resolved": row.resolved,
                    "correlation_id": row.correlation_id,
                    "created_at": _iso(row.created_at),
                }
                for row in error_rows
            ],
        },
    }
