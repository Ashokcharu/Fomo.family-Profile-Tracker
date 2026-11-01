from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.features.security_analysis.service import SecurityAnalysisService
from app.models import SecurityAnalysis, Token

logger = logging.getLogger(__name__)


async def run_security_analysis(interval_seconds: int = 300, batch_size: int = 10) -> None:
    interval = max(60, int(interval_seconds))
    limit = max(1, int(batch_size))
    while True:
        await asyncio.sleep(interval)
        if not get_settings().security_analysis_enabled:
            continue
        try:
            await asyncio.to_thread(run_security_batch, limit)
        except Exception:
            logger.exception("Scheduled security analysis batch failed")


def run_security_batch(batch_size: int = 10) -> int:
    settings = get_settings()
    with SessionLocal() as db:
        stale_after = datetime.now(timezone.utc) - timedelta(seconds=settings.security_analysis_cache_ttl_seconds)
        recently_scanned = select(SecurityAnalysis.token_id).where(
            SecurityAnalysis.last_analyzed_at.is_not(None),
            SecurityAnalysis.last_analyzed_at >= stale_after,
        )
        tokens = (
            db.query(Token)
            .filter(~Token.id.in_(recently_scanned))
            .order_by(Token.created_at.desc(), Token.id.desc())
            .limit(max(1, int(batch_size)))
            .all()
        )
        if not tokens:
            return 0

        service = SecurityAnalysisService()
        processed = 0
        for token in tokens:
            service.analyze_token(db, token.id)
            processed += 1
        db.commit()
        return processed
