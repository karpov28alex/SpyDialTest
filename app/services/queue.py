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
    """Persist a job reliably.

    PostgreSQL is the source of truth. We deliberately do not push the job ID
    to Redis before the surrounding transaction commits: a worker could consume
    that ID immediately and fail to see the uncommitted database row. The worker
    continuously discovers committed queued jobs and then uses Redis only as a
    wake-up/transport mechanism.
    """
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
    return job
