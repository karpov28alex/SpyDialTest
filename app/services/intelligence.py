from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import case, extract, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Dialog, Media, Message, User
from app.services.access import access_state


def _contact(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "dialog_id": int(row.dialog_id),
        "name": row.peer_name or row.peer_username or "Без имени",
        "username": row.peer_username,
        "value": int(row.value or 0),
    }


async def build_user_intelligence(
    session: AsyncSession,
    user: User,
    *,
    days: int = 30,
) -> dict[str, Any]:
    days = max(7, min(int(days), 3650))
    now = datetime.now(UTC)
    since = now - timedelta(days=days)
    dialog_filter = Dialog.owner_user_id == user.id

    totals_row = (
        await session.execute(
            select(
                func.count(func.distinct(Dialog.id)).label("dialogs"),
                func.count(Message.id).label("messages"),
                func.count(Media.id).label("media"),
                func.sum(case((Message.is_deleted.is_(True), 1), else_=0)).label("deleted"),
                func.sum(case((Message.edited_at.is_not(None), 1), else_=0)).label("edited"),
                func.sum(case((Media.is_protected.is_(True), 1), else_=0)).label("protected"),
                func.min(Message.sent_at).label("first_message_at"),
                func.max(Message.sent_at).label("last_message_at"),
            )
            .select_from(Dialog)
            .outerjoin(Message, Message.dialog_id == Dialog.id)
            .outerjoin(Media, Media.message_id == Message.id)
            .where(dialog_filter)
        )
    ).one()

    period_rows = (
        await session.execute(
            select(
                func.date(Message.sent_at).label("day"),
                func.count(Message.id).label("messages"),
                func.sum(case((Message.is_deleted.is_(True), 1), else_=0)).label("deleted"),
                func.sum(case((Message.edited_at.is_not(None), 1), else_=0)).label("edited"),
            )
            .join(Dialog, Dialog.id == Message.dialog_id)
            .where(Dialog.owner_user_id == user.id, Message.sent_at >= since)
            .group_by(func.date(Message.sent_at))
            .order_by(func.date(Message.sent_at))
        )
    ).all()

    hourly_rows = (
        await session.execute(
            select(
                extract("hour", Message.sent_at).label("hour"),
                func.count(Message.id).label("messages"),
            )
            .join(Dialog, Dialog.id == Message.dialog_id)
            .where(Dialog.owner_user_id == user.id, Message.sent_at >= since)
            .group_by(extract("hour", Message.sent_at))
            .order_by(extract("hour", Message.sent_at))
        )
    ).all()

    async def leader(value_expr, *extra_where):
        return (
            await session.execute(
                select(
                    Dialog.id.label("dialog_id"),
                    Dialog.peer_name,
                    Dialog.peer_username,
                    value_expr.label("value"),
                )
                .join(Message, Message.dialog_id == Dialog.id)
                .outerjoin(Media, Media.message_id == Message.id)
                .where(Dialog.owner_user_id == user.id, *extra_where)
                .group_by(Dialog.id, Dialog.peer_name, Dialog.peer_username)
                .order_by(value_expr.desc(), Dialog.id)
                .limit(1)
            )
        ).first()

    active = await leader(func.count(func.distinct(Message.id)))
    media_leader = await leader(func.count(Media.id), Media.id.is_not(None))
    deleted_leader = await leader(
        func.sum(case((Message.is_deleted.is_(True), 1), else_=0)),
        Message.is_deleted.is_(True),
    )
    protected_leader = await leader(
        func.sum(case((Media.is_protected.is_(True), 1), else_=0)),
        Media.is_protected.is_(True),
    )

    longest = (
        await session.execute(
            select(
                Dialog.id.label("dialog_id"),
                Dialog.peer_name,
                Dialog.peer_username,
                func.count(Message.id).label("value"),
                func.min(Message.sent_at).label("started_at"),
                func.max(Message.sent_at).label("last_at"),
            )
            .join(Message, Message.dialog_id == Dialog.id)
            .where(Dialog.owner_user_id == user.id)
            .group_by(Dialog.id, Dialog.peer_name, Dialog.peer_username)
            .order_by(func.count(Message.id).desc())
            .limit(1)
        )
    ).first()

    access = await access_state(session, user)
    locked = not access.active
    totals = {
        "dialogs": int(totals_row.dialogs or 0),
        "messages": int(totals_row.messages or 0),
        "media": int(totals_row.media or 0),
        "deleted": int(totals_row.deleted or 0),
        "edited": int(totals_row.edited or 0),
        "protected": int(totals_row.protected or 0),
    }

    peak_hour = max(hourly_rows, key=lambda row: int(row.messages or 0), default=None)
    insights: list[str] = []
    if active:
        insights.append(f"Чаще всего вы общаетесь с {active.peer_name or active.peer_username or 'одним из собеседников'}.")
    if media_leader:
        insights.append(f"Больше всего медиа связано с {media_leader.peer_name or media_leader.peer_username or 'одним из диалогов'}.")
    if deleted_leader:
        insights.append(f"Чаще остальных сообщения удаляет {deleted_leader.peer_name or deleted_leader.peer_username or 'один из собеседников'}.")
    if peak_hour is not None:
        insights.append(f"Пик активности приходится примерно на {int(peak_hour.hour):02d}:00.")
    if totals["edited"]:
        insights.append(f"В архиве сохранено {totals['edited']} изменений сообщений.")
    if not insights:
        insights.append("Пока недостаточно данных для персональных выводов.")

    def maybe_contact(row):
        contact = _contact(row)
        if contact and locked:
            contact["name"] = "********"
            contact["username"] = None
        return contact

    longest_payload = None
    if longest:
        started = longest.started_at
        last = longest.last_at
        longest_payload = maybe_contact(longest)
        longest_payload.update(
            {
                "started_at": started,
                "last_at": last,
                "days": max((last - started).days, 0) if started and last else 0,
            }
        )

    return {
        "generated_at": now,
        "period_days": days,
        "locked": locked,
        "access": {"active": access.active, "source": access.source, "ends_at": access.ends_at},
        "totals": totals,
        "activity": [
            {
                "date": str(row.day),
                "messages": int(row.messages or 0),
                "deleted": int(row.deleted or 0),
                "edited": int(row.edited or 0),
            }
            for row in period_rows
        ],
        "hours": [{"hour": int(row.hour), "messages": int(row.messages or 0)} for row in hourly_rows],
        "leaders": {
            "active": maybe_contact(active),
            "media": maybe_contact(media_leader),
            "deleted": maybe_contact(deleted_leader),
            "protected": maybe_contact(protected_leader),
            "longest": longest_payload,
        },
        "insights": insights[:5] if not locked else ["Подробные персональные выводы доступны после оплаты."],
    }
