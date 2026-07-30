import asyncio
import json
from datetime import UTC, datetime, timedelta

import structlog
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import FSInputFile
from redis.asyncio import Redis
from sqlalchemy import select, update

from app.bot.setup import bot
from app.business.events import is_protected_message
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.models import Job, Media, Message, User
from app.db.session import SessionLocal
from app.services.broadcasts import send_broadcast
from app.services.media import download_telegram_file, safe_media_path
from app.services.queue import QUEUE_KEY

settings = get_settings()
logger = structlog.get_logger()
QUEUE_MARKER_PREFIX = "dialog_spy:job_enqueued:"
STALE_RUNNING_SECONDS = 300


async def _ensure_media_downloaded(media_id: int) -> Media:
    async with SessionLocal() as session, session.begin():
        media = await session.get(Media, media_id, with_for_update=True)
        if not media:
            raise RuntimeError("Media not found")
        message = await session.get(Message, media.message_id)
        if not message:
            raise RuntimeError("Media message not found")
        raw = message.raw_metadata or {}
        decision = is_protected_message(type("StoredTelegramMessage", (), {
            "has_protected_content": raw.get("has_protected_content", False),
            "model_dump": lambda self, **kwargs: raw,
        })())
        embedded_capture = raw.get("_capture_reason") == "embedded_reply_missing_original"
        if media.is_protected is not True or not (decision.allowed or embedded_capture):
            media.is_protected = False
            raise RuntimeError("Protected media invariant failed: no Telegram protection signal")
        if media.download_status == "downloaded" and media.storage_key:
            return media
        storage_key = f"messages/{media.message_id}/{media.id}"
        checksum, size = await download_telegram_file(bot, media.telegram_file_id, storage_key, settings)
        media.storage_key = storage_key
        media.checksum = checksum
        media.size = media.size or size
        media.download_status = "downloaded"
        media.downloaded_at = datetime.now(UTC)
        return media


async def _deliver_protected_media(job: Job) -> None:
    media_id = int(job.payload["media_id"])
    media = await _ensure_media_downloaded(media_id)
    async with SessionLocal() as session:
        user = await session.get(User, int(job.payload["owner_user_id"]))
        if not user:
            raise RuntimeError("Owner not found")
        if not media.storage_key:
            raise RuntimeError("Downloaded media has no storage key")
        path = safe_media_path(settings, media.storage_key)
        caption = (
            "🔐 <b>Скрытое медиа сохранено</b>\n\n"
            f"💬 <b>Диалог:</b> {job.payload.get('dialog_name') or 'Без имени'}\n"
            f"📎 <b>Тип:</b> {media.media_type}\n\n"
            "Медиа сохранено после вашего ответа на одноразовое сообщение. Его можно переслать или сохранить."
        )
        filename = media.filename or f"protected-{media.id}"
        file = FSInputFile(path, filename=filename)
        if media.media_type == "photo":
            await bot.send_photo(user.telegram_id, photo=file, caption=caption, protect_content=False)
        elif media.media_type in {"video", "animation"}:
            await bot.send_video(user.telegram_id, video=file, caption=caption, protect_content=False)
        elif media.media_type == "voice":
            await bot.send_voice(user.telegram_id, voice=file, caption=caption, protect_content=False)
        elif media.media_type == "video_note":
            await bot.send_video_note(user.telegram_id, video_note=file, protect_content=False)
            await bot.send_message(user.telegram_id, caption)
        elif media.media_type == "audio":
            await bot.send_audio(user.telegram_id, audio=file, caption=caption, protect_content=False)
        elif media.media_type == "sticker":
            await bot.send_sticker(user.telegram_id, sticker=file, protect_content=False)
            await bot.send_message(user.telegram_id, caption)
        else:
            await bot.send_document(user.telegram_id, document=file, caption=caption, protect_content=False)


async def handle_job(job: Job) -> None:
    if job.kind == "send_text":
        await bot.send_message(job.payload["telegram_id"], job.payload["text"])
        return
    if job.kind == "broadcast_send":
        await send_broadcast(bot, job.payload)
        return
    if job.kind == "download_media":
        async with SessionLocal() as session, session.begin():
            media = await session.get(Media, int(job.payload["media_id"]), with_for_update=True)
            if not media or media.download_status == "downloaded":
                return
            storage_key = f"messages/{media.message_id}/{media.id}"
            checksum, size = await download_telegram_file(bot, media.telegram_file_id, storage_key, settings)
            media.storage_key = storage_key
            media.checksum = checksum
            media.size = media.size or size
            media.download_status = "downloaded"
            media.downloaded_at = datetime.now(UTC)
        return
    if job.kind in {"send_protected_media", "deliver_protected_media"}:
        await _deliver_protected_media(job)
        return
    raise RuntimeError(f"Unknown job kind: {job.kind}")


async def process_job(job_id: int, redis: Redis) -> None:
    async with SessionLocal() as session, session.begin():
        job = await session.scalar(select(Job).where(Job.id == job_id).with_for_update(skip_locked=True))
        if not job or job.status in {"done", "dead", "running"}:
            await redis.delete(f"{QUEUE_MARKER_PREFIX}{job_id}")
            return
        if job.available_at > datetime.now(UTC):
            await redis.delete(f"{QUEUE_MARKER_PREFIX}{job_id}")
            return
        job.status = "running"
        job.locked_at = datetime.now(UTC)
        job.attempts += 1
    try:
        async with SessionLocal() as session:
            job = await session.get(Job, job_id)
            if job:
                await handle_job(job)
        async with SessionLocal() as session, session.begin():
            job = await session.get(Job, job_id, with_for_update=True)
            if job:
                job.status = "done"
                job.last_error = None
                job.locked_at = None
    except TelegramRetryAfter as exc:
        await reschedule(job_id, str(exc), max(int(exc.retry_after), 1))
    except TelegramForbiddenError as exc:
        async with SessionLocal() as session, session.begin():
            job = await session.get(Job, job_id, with_for_update=True)
            if job:
                job.status = "dead"
                job.last_error = str(exc)
                job.locked_at = None
                telegram_id = job.payload.get("telegram_id")
                if telegram_id:
                    user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
                    if user:
                        user.blocked_bot_at = datetime.now(UTC)
    except TelegramBadRequest as exc:
        await reschedule(job_id, str(exc))
    except Exception as exc:
        logger.exception("job_failed", job_id=job_id, kind=getattr(job, "kind", None))
        await reschedule(job_id, str(exc))
    finally:
        await redis.delete(f"{QUEUE_MARKER_PREFIX}{job_id}")


async def reschedule(job_id: int, error: str, delay: int | None = None) -> None:
    async with SessionLocal() as session, session.begin():
        job = await session.get(Job, job_id, with_for_update=True)
        if not job:
            return
        job.locked_at = None
        if job.attempts >= job.max_attempts:
            job.status = "dead"
        else:
            retry_delay = delay or min(2**job.attempts, 300)
            job.status = "queued"
            job.available_at = datetime.now(UTC) + timedelta(seconds=retry_delay)
        job.last_error = error


async def recover_stale_running_jobs() -> int:
    threshold = datetime.now(UTC) - timedelta(seconds=STALE_RUNNING_SECONDS)
    async with SessionLocal() as session, session.begin():
        result = await session.execute(update(Job).where(Job.status == "running", Job.locked_at < threshold).values(status="queued", locked_at=None, available_at=datetime.now(UTC)))
        return int(result.rowcount or 0)


async def recover_queued_jobs(redis: Redis) -> int:
    async with SessionLocal() as session:
        ids = list((await session.scalars(select(Job.id).where(Job.status == "queued", Job.available_at <= datetime.now(UTC)).order_by(Job.id).limit(100))).all())
    queued = 0
    for job_id in ids:
        marker = f"{QUEUE_MARKER_PREFIX}{job_id}"
        if await redis.set(marker, "1", ex=60, nx=True):
            await redis.lpush(QUEUE_KEY, json.dumps({"job_id": job_id}))
            queued += 1
    return queued


async def main() -> None:
    configure_logging()
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    logger.info("worker_started", version=settings.app_version, git_sha=settings.git_sha)
    while True:
        try:
            stale = await recover_stale_running_jobs()
            if stale:
                logger.warning("stale_jobs_recovered", count=stale)
            await recover_queued_jobs(redis)
            item = await redis.brpop(QUEUE_KEY, timeout=2)
            if item:
                payload = json.loads(item[1])
                await process_job(int(payload["job_id"]), redis)
        except Exception:
            logger.exception("worker_loop_error")
            await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(main())
