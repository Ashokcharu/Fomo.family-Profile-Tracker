from __future__ import annotations

from app.features.tokens.service import token_display_data
from sqlalchemy.orm import Session

from app.models import DiscoveryEvent, Token


def calculate_discovery_score(signals: dict) -> float:
    """Explainable 0-100 score. Missing signals are ignored rather than treated as positive."""
    score = 0.0

    unique_buyers = signals.get("unique_buyers", 0) or 0
    market_cap = signals.get("market_cap")
    liquidity = signals.get("liquidity")
    freshness_seconds = signals.get("freshness_seconds")
    age_minutes = signals.get("age_minutes", 0) or 0
    risk = signals.get("risk", "unknown")
    buy_cluster_window = signals.get("buy_cluster_window_seconds")

    if unique_buyers >= 2:
        score += 25
    elif unique_buyers == 1:
        score += 8

    if buy_cluster_window is not None and buy_cluster_window <= 15 * 60:
        score += 10

    if market_cap is not None:
        if 10_000 <= market_cap <= 30_000:
            score += 20
        elif 30_000 < market_cap <= 50_000:
            score += 10

    if liquidity is not None:
        if liquidity >= 5_000:
            score += 15
        elif liquidity >= 1_000:
            score += 5

    if age_minutes >= 15:
        score += 10
    elif age_minutes >= 5:
        score += 5

    if freshness_seconds is not None:
        if freshness_seconds <= 60:
            score += 10
        elif freshness_seconds <= 180:
            score += 4

    if risk == "low":
        score += 10
    elif risk == "medium":
        score += 5

    return round(min(score, 100.0), 2)


def is_reeligible_after_dismissal(is_dismissed: bool, chain: str | None = None, token_address: str | None = None) -> bool:
    """Soft dismissals prevent reentry. The chain/address are kept available for future bookkeeping but not used as positive evidence."""
    return not bool(is_dismissed)


def list_discoveries(db: Session, limit: int = 20) -> list[dict]:
    rows = db.query(DiscoveryEvent).order_by(DiscoveryEvent.discovered_at.desc()).limit(limit).all()
    token_data = token_display_data(db, [row.token_id for row in rows])
    return [
        {
            "id": row.id,
            "token_id": row.token_id,
            "source": row.source,
            "provider_name": row.provider_name,
            "confidence": row.confidence,
            "initial_market_cap": row.initial_market_cap,
            "discovered_at": row.discovered_at.isoformat(),
            **token_data.get(row.token_id, {}),
        }
        for row in rows
    ]


def create_discovery(db: Session, token: Token, source: str, provider_name: str | None = None, market_cap: float | None = None) -> DiscoveryEvent:
    event = DiscoveryEvent(
        token_id=token.id,
        source=source,
        provider_name=provider_name,
        initial_market_cap=market_cap,
        confidence=0.5,
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event
