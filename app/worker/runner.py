import asyncio
import json
from datetime import UTC, datetime, timedelta

import structlog
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import FSInputFile
from redis.asyncio import Redis
from sqlalchemy import select

from app.bot.setup import bot
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.models import Job, Media, User
from app.db.session import SessionLocal
from app.services.media import download_telegram_file, safe_media_path
from app.services.queue import QUEUE_KEY

settings = get_settings()
logger = structlog.get_logger()


async def _ensure_media_downloaded(media_id: int) -> Media:
    async with SessionLocal() as session, session.begin():
        media = await session.get(Media, media_id, with_for_update=True)
        if not media:
            raise RuntimeError("Media not found")
        if media.is_protected is not True:
            raise RuntimeError("Protected media invariant failed")
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


async def handle_job(job: Job) -> None:
    if job.kind == "send_text":
        await bot.send_message(job.payload["telegram_id"], job.payload["text"])
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
        media_id = int(job.payload["media_id"])
        media = await _ensure_media_downloaded(media_id)
        async with SessionLocal() as session:
            user = await session.get(User, int(job.payload["owner_user_id"]))
            if not user:
                raise RuntimeError("Owner not found")
            path = safe_media_path(settings, media.storage_key)
            caption = (
                "🔐 <b>Защищённое медиа сохранено</b>\n\n"
                f"<b>Диалог:</b> {job.payload.get('dialog_name') or 'Без имени'}\n"
                f"<b>Тип:</b> {media.media_type}\n\n"
                "Копия сохранена до открытия исходного сообщения."
            )
            await bot.send_document(
                user.telegram_id,
                document=FSInputFile(path, filename=media.filename or f"protected-{media.id}"),
                caption=caption,
                protect_content=True,
            )
        return

    raise RuntimeError(f"Unknown job kind: {job.kind}")


async def process_job(job_id: int) -> None:
    async with SessionLocal() as session, session.begin():
        job = await session.scalar(select(Job).where(Job.id == job_id).with_for_update(skip_locked=True))
        if not job or job.status in {"done", "dead"} or job.available_at > datetime.now(UTC):
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
    except TelegramRetryAfter as exc:
        await reschedule(job_id, str(exc), max(int(exc.retry_after), 1))
    except TelegramForbiddenError as exc:
        async with SessionLocal() as session, session.begin():
            job = await session.get(Job, job_id, with_for_update=True)
            if job:
                job.status = "dead"
                job.last_error = str(exc)
            telegram_id = job.payload.get("telegram_id") if job else None
            if telegram_id:
                user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
                if user:
                    user.blocked_bot_at = datetime.now(UTC)
    except TelegramBadRequest as exc:
        # TelegramBadRequest может быть временным для ещё не скачанного файла.
        await reschedule(job_id, str(exc))
    except Exception as exc:
        await reschedule(job_id, str(exc))


async def reschedule(job_id: int, error: str, delay: int | None = None) -> None:
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    async with SessionLocal() as session, session.begin():
        job = await session.get(Job, job_id, with_for_update=True)
        if not job:
            return
        if job.attempts >= job.max_attempts:
            job.status = "dead"
        else:
            delay = delay or min(2 ** job.attempts, 300)
            job.status = "queued"
            job.available_at = datetime.now(UTC) + timedelta(seconds=delay)
            await redis.lpush(QUEUE_KEY, json.dumps({"job_id": job.id}))
        job.last_error = error
    await redis.aclose()


async def recover_queued_jobs(redis: Redis) -> None:
    """Подбирает задания, потерянные из-за гонки commit PostgreSQL / Redis."""
    async with SessionLocal() as session:
        ids = list((await session.scalars(
            select(Job.id)
            .where(Job.status == "queued", Job.available_at <= datetime.now(UTC))
            .order_by(Job.id)
            .limit(100)
        )).all())
    for job_id in ids:
        await redis.lpush(QUEUE_KEY, json.dumps({"job_id": job_id}))


async def main() -> None:
    configure_logging()
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    logger.info("worker_started", version=settings.app_version, git_sha=settings.git_sha)
    while True:
        item = await redis.brpop(QUEUE_KEY, timeout=3)
        try:
            if item:
                payload = json.loads(item[1])
                await process_job(int(payload["job_id"]))
            else:
                await recover_queued_jobs(redis)
        except Exception:
            logger.exception("worker_loop_error")
            await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(main())
