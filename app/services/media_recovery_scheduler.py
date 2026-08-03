from __future__ import annotations

import asyncio

import structlog

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.services.media_backfill import backfill_media

logger = structlog.get_logger()


async def media_recovery_loop() -> None:
    settings = get_settings()
    while True:
        try:
            async with SessionLocal() as session:
                result = await backfill_media(
                    session,
                    settings,
                    limit=20,
                    include_missing_files=True,
                )
            selected = int(result.get("selected") or 0)
            restored = int(result.get("restored") or 0)
            failed = int(result.get("failed") or 0)
            if selected:
                logger.info(
                    "media_recovery_batch_finished",
                    selected=selected,
                    restored=restored,
                    failed=failed,
                    errors=result.get("errors", []),
                )
            await asyncio.sleep(20 if restored else 90)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("media_recovery_loop_failed")
            await asyncio.sleep(60)
