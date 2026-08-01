from __future__ import annotations

import csv
import html
import io
import json

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.api.deps import CurrentUser, SessionDep
from app.db.models import Dialog, Media, Message
from app.services.access import access_state
from app.services.intelligence import build_user_intelligence

router = APIRouter(prefix="/api/intelligence", tags=["user-intelligence"])


@router.get("")
async def intelligence(
    user: CurrentUser,
    session: SessionDep,
    days: int = Query(30, ge=7, le=3650),
) -> dict:
    return await build_user_intelligence(session, user, days=days)


async def _export_rows(session: SessionDep, user_id: int):
    rows = (
        await session.execute(
            select(Dialog, Message)
            .join(Message, Message.dialog_id == Dialog.id)
            .where(Dialog.owner_user_id == user_id)
            .order_by(Message.sent_at, Message.id)
        )
    ).all()
    message_ids = [message.id for _, message in rows]
    media_rows = []
    if message_ids:
        media_rows = list((await session.scalars(select(Media).where(Media.message_id.in_(message_ids)))).all())
    media_by_message: dict[int, list[str]] = {}
    for item in media_rows:
        media_by_message.setdefault(item.message_id, []).append(item.media_type)
    return rows, media_by_message


@router.get("/export/{format_name}")
async def export_archive(
    format_name: str,
    user: CurrentUser,
    session: SessionDep,
):
    state = await access_state(session, user)
    if not state.active:
        raise HTTPException(status_code=402, detail="Экспорт доступен после оплаты")
    if format_name not in {"csv", "json", "html"}:
        raise HTTPException(status_code=404, detail="Формат экспорта не поддерживается")

    rows, media_by_message = await _export_rows(session, user.id)
    filename = f"phantom-archive-{user.telegram_id}.{format_name}"

    if format_name == "csv":
        stream = io.StringIO()
        writer = csv.writer(stream, delimiter=";")
        writer.writerow([
            "dialog_id", "name", "username", "message_id", "direction", "sent_at",
            "edited_at", "deleted_at", "is_deleted", "text", "caption", "media",
        ])
        for dialog, message in rows:
            writer.writerow([
                dialog.id,
                dialog.peer_name or "",
                dialog.peer_username or "",
                message.telegram_message_id,
                message.direction,
                message.sent_at.isoformat() if message.sent_at else "",
                message.edited_at.isoformat() if message.edited_at else "",
                message.deleted_at.isoformat() if message.deleted_at else "",
                "1" if message.is_deleted else "0",
                message.text or "",
                message.caption or "",
                ",".join(media_by_message.get(message.id, [])),
            ])
        payload = "\ufeff" + stream.getvalue()
        return StreamingResponse(
            iter([payload.encode("utf-8")]),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    items = [
        {
            "dialog": {
                "id": dialog.id,
                "name": dialog.peer_name,
                "username": dialog.peer_username,
                "telegram_chat_id": dialog.telegram_chat_id,
            },
            "message": {
                "id": message.telegram_message_id,
                "direction": message.direction,
                "text": message.text,
                "caption": message.caption,
                "sent_at": message.sent_at.isoformat() if message.sent_at else None,
                "edited_at": message.edited_at.isoformat() if message.edited_at else None,
                "deleted_at": message.deleted_at.isoformat() if message.deleted_at else None,
                "is_deleted": message.is_deleted,
                "media": media_by_message.get(message.id, []),
            },
        }
        for dialog, message in rows
    ]

    if format_name == "json":
        payload = json.dumps(
            {"telegram_id": user.telegram_id, "items": items},
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
        return StreamingResponse(
            iter([payload]),
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    cards = []
    for item in items:
        dialog = item["dialog"]
        message = item["message"]
        body = message["text"] or message["caption"] or "[медиа или пустое сообщение]"
        flags = []
        if message["edited_at"]:
            flags.append("изменено")
        if message["is_deleted"]:
            flags.append("удалено")
        if message["media"]:
            flags.append("медиа: " + ", ".join(message["media"]))
        cards.append(
            "<article class='message'>"
            f"<h3>{html.escape(dialog['name'] or dialog['username'] or 'Диалог')}</h3>"
            f"<div class='meta'>{html.escape(message['sent_at'] or '')} · {html.escape(message['direction'] or '')}</div>"
            f"<p>{html.escape(body)}</p>"
            f"<small>{html.escape(' · '.join(flags))}</small>"
            "</article>"
        )
    document = f"""<!doctype html><html lang='ru'><head><meta charset='utf-8'>
<title>Phantom Archive</title><style>
body{{margin:0;padding:24px;background:#08060d;color:#fff;font:15px/1.5 system-ui}}
main{{max-width:900px;margin:auto}}.message{{padding:18px;margin:12px 0;border:1px solid #4b2b67;border-radius:18px;background:#151020}}
h1{{color:#c88cff}}h3{{margin:0 0 4px}}.meta,small{{color:#ad9dbc}}p{{white-space:pre-wrap}}
</style></head><body><main><h1>PHANTOM · Архив</h1><p>Telegram ID: {user.telegram_id}</p>{''.join(cards) or '<p>Сообщений пока нет.</p>'}</main></body></html>"""
    return StreamingResponse(
        iter([document.encode("utf-8")]),
        media_type="text/html; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
