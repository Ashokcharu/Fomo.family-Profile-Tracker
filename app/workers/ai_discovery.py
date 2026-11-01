from __future__ import annotations

import asyncio
import logging

from app.config import get_settings
from app.database import SessionLocal
from app.features.ai_discovery.service import AIDataService
from app.models import BackgroundJob

logger = logging.getLogger(__name__)


async def run_ai_discovery(interval_seconds: int = 600) -> None:
    interval = max(60, int(interval_seconds))
    while True:
        await asyncio.sleep(interval)
        if not get_settings().ai_discovery_enabled:
            continue
        try:
            await asyncio.to_thread(run_automatic_ai_discovery)
        except Exception:
            logger.exception("Scheduled AI Discovery scan failed")


def run_automatic_ai_discovery() -> None:
    with SessionLocal() as db:
        active_job = (
            db.query(BackgroundJob.id)
            .filter(
                BackgroundJob.job_type == "ai_discovery",
                BackgroundJob.status.in_(("queued", "running", "model_pending")),
            )
            .first()
        )
        if active_job:
            logger.info("Skipping scheduled AI Discovery because a manual scan is active")
            return
        result = AIDataService().run_discovery(db, use_model=False)
        logger.info("Automatically ranked %d active tokens for AI Discovery", result["tokens_scanned"])