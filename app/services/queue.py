import json
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Job

QUEUE_KEY = "dialog_spy:jobs"


async def enqueue_job(
    session: AsyncSession,
    redis: Redis,
    *,
    kind: str,
    payload: dict[str, Any],
    idempotency_key: str,
) -> Job:
    existing = await session.scalar(select(Job).where(Job.idempotency_key == idempotency_key))
    if existing:
        return existing
    job = Job(
        kind=kind,
        payload=payload,
        status="queued",
        available_at=datetime.now(UTC),
        idempotency_key=idempotency_key,
    )
    session.add(job)
    await session.flush()
    await redis.lpush(QUEUE_KEY, json.dumps({"job_id": job.id}))
    return job
