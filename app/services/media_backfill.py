from __future__ import annotations

import hashlib
import mimetypes
import re
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import Media
from app.services.media import copy_or_download_telegram_file, safe_media_path
from app.services.telegram_bot import build_bot

_SAFE_NAME = re.compile(r"[^a-zA-Z0-9._-]+")


def _filename(media: Media, telegram_path: str | None) -> str:
    original = Path(media.filename or "").name
    if original:
        cleaned = _SAFE_NAME.sub("_", original).strip("._")
        if cleaned:
            return cleaned[:220]
    suffix = Path(telegram_path or "").suffix
    if not suffix and media.mime_type:
        suffix = mimetypes.guess_extension(media.mime_type) or ""
    return f"media-{media.id}{suffix}"


def _failure_status(message: str, *, local_api_enabled: bool) -> str:
    lowered = message.lower()
    if "wrong file_id" in lowered or "temporarily unavailable" in lowered:
        return "unavailable"
    if "file is too big" in lowered:
        return "failed" if local_api_enabled else "requires_local_api"
    return "failed"


async def restore_media_file(bot, media: Media, settings: Settings) -> tuple[str, int, str]:
    telegram_file = await bot.get_file(media.telegram_file_id)
    if not telegram_file.file_path:
        raise RuntimeError("Telegram getFile response does not contain file_path")

    filename = _filename(media, telegram_file.file_path)
    storage_key = media.storage_key or f"archive/{media.message_id}/{media.id}-{filename}"
    destination = safe_media_path(settings, storage_key)

    await copy_or_download_telegram_file(
        bot,
        telegram_file.file_path,
        destination,
        settings,
    )

    digest = hashlib.sha256()
    size = 0
    with destination.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size, storage_key


async def backfill_media(
    session: AsyncSession,
    settings: Settings,
    *,
    limit: int = 50,
    include_missing_files: bool = True,
) -> dict[str, object]:
    retry_statuses = ["pending", "failed", "error"]
    if settings.telegram_api_base_url and settings.telegram_api_is_local:
        retry_statuses.append("requires_local_api")

    rows = list((await session.scalars(
        select(Media)
        .where(or_(
            Media.download_status.in_(retry_statuses),
            Media.storage_key.is_(None),
        ))
        .order_by(Media.id)
        .limit(limit)
    )).all())

    if include_missing_files and len(rows) < limit:
        downloaded = list((await session.scalars(
            select(Media)
            .where(Media.download_status == "downloaded", Media.storage_key.is_not(None))
            .order_by(Media.id)
            .limit(limit * 4)
        )).all())
        known = {row.id for row in rows}
        for media in downloaded:
            if media.id in known:
                continue
            try:
                exists = safe_media_path(settings, media.storage_key or "").is_file()
            except ValueError:
                exists = False
            if not exists:
                rows.append(media)
                known.add(media.id)
                if len(rows) >= limit:
                    break

    restored = 0
    failed = 0
    unavailable = 0
    requires_local_api = 0
    errors: list[dict[str, object]] = []
    bot = build_bot(settings)
    local_api_enabled = bool(settings.telegram_api_base_url and settings.telegram_api_is_local)
    try:
        for media in rows:
            try:
                media.download_status = "downloading"
                await session.flush()
                checksum, size, storage_key = await restore_media_file(bot, media, settings)
                media.storage_key = storage_key
                media.checksum = checksum
                media.size = size
                media.download_status = "downloaded"
                media.downloaded_at = datetime.now(UTC)
                restored += 1
            except Exception as exc:  # one broken Telegram file must not stop the archive
                status = _failure_status(str(exc), local_api_enabled=local_api_enabled)
                media.download_status = status
                if status == "unavailable":
                    unavailable += 1
                elif status == "requires_local_api":
                    requires_local_api += 1
                else:
                    failed += 1
                errors.append({"media_id": media.id, "status": status, "error": str(exc)[:500]})
            await session.commit()
    finally:
        await bot.session.close()

    return {
        "selected": len(rows),
        "restored": restored,
        "failed": failed,
        "unavailable": unavailable,
        "requires_local_api": requires_local_api,
        "errors": errors[:20],
    }
