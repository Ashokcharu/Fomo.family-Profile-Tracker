from __future__ import annotations

import math
import statistics
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import FollowedTrader, MarketObservation, Token, TraderTokenPick, TraderTransactionEvent

PERFORMANCE_MARKET_CAP_LIMIT_USD = 50_000.0
FAILURE_OBSERVATION_PERIOD = timedelta(days=7)
MIN_FAILURE_OBSERVATIONS = 3


def record_trader_token_pick(
    db: Session,
    trader_id: int,
    token_id: int,
    market_cap: float | None,
    observed_at: datetime,
) -> None:
    if not _qualifying_market_cap(market_cap):
        return
    existing = db.query(TraderTokenPick.id).filter_by(
        followed_trader_id=trader_id,
        token_id=token_id,
    ).first()
    if existing is not None:
        return
    db.add(TraderTokenPick(
        followed_trader_id=trader_id,
        token_id=token_id,
        reached_market_cap=market_cap,
        reached_high_market_cap=market_cap,
        current_market_cap=market_cap,
        first_observed_at=observed_at,
        last_observed_at=observed_at,
        observation_count=1,
    ))
    db.flush()


def update_token_pick_market_caps(
    db: Session,
    token_id: int,
    market_cap: float | None,
    observed_at: datetime,
) -> None:
    if market_cap is None or not math.isfinite(market_cap) or market_cap < 0:
        return
    observed_at = _as_utc(observed_at)
    for pick in db.query(TraderTokenPick).filter_by(token_id=token_id).all():
        if observed_at <= _as_utc(pick.last_observed_at):
            continue
        pick.current_market_cap = market_cap
        pick.reached_high_market_cap = max(pick.reached_high_market_cap, market_cap)
        pick.last_observed_at = observed_at
        pick.observation_count += 1


def backfill_trader_token_picks(db: Session) -> int:
    events = (
        db.query(TraderTransactionEvent)
        .filter(
            TraderTransactionEvent.followed_trader_id.is_not(None),
            TraderTransactionEvent.event_type == "buy",
            TraderTransactionEvent.source == "fomo_family",
        )
        .order_by(
            func.coalesce(TraderTransactionEvent.event_time, TraderTransactionEvent.detection_timestamp),
            TraderTransactionEvent.id,
        )
        .all()
    )
    inserted = 0
    for event in events:
        market_cap = event.market_cap_at_event
        if market_cap is None and event.event_time is not None:
            market_cap = db.query(MarketObservation.market_cap).filter(
                MarketObservation.token_id == event.token_id,
                MarketObservation.source == "fomo_family_discovery",
                MarketObservation.observed_at == event.event_time,
                MarketObservation.market_cap.is_not(None),
            ).order_by(MarketObservation.id.asc()).limit(1).scalar()
        if not _qualifying_market_cap(market_cap):
            continue
        existed = db.query(TraderTokenPick.id).filter_by(
            followed_trader_id=event.followed_trader_id,
            token_id=event.token_id,
        ).first()
        if existed is not None:
            continue
        record_trader_token_pick(
            db,
            event.followed_trader_id,
            event.token_id,
            market_cap,
            event.event_time or event.detection_timestamp,
        )
        inserted += 1
    if inserted:
        db.commit()
    return inserted


def trader_performance_stats(db: Session, trader_ids: list[int]) -> dict[int, dict]:
    if not trader_ids:
        return {}
    rows = (
        db.query(TraderTokenPick, Token)
        .join(Token, Token.id == TraderTokenPick.token_id)
        .filter(TraderTokenPick.followed_trader_id.in_(trader_ids))
        .all()
    )
    picks_by_trader: dict[int, list[tuple[TraderTokenPick, Token, float]]] = {}
    for pick, token in rows:
        growth = pick.reached_high_market_cap / pick.reached_market_cap
        if not math.isfinite(growth):
            continue
        picks_by_trader.setdefault(pick.followed_trader_id, []).append((pick, token, growth))

    result = {}
    for trader_id in trader_ids:
        picks = picks_by_trader.get(trader_id, [])
        total = len(picks)
        if not total:
            result[trader_id] = _empty_performance()
            continue
        growths = [growth for _, _, growth in picks]
        success_2x = sum(growth >= 2 for growth in growths) / total * 100
        success_5x = sum(growth >= 5 for growth in growths) / total * 100
        success_10x = sum(growth >= 10 for growth in growths) / total * 100
        average_growth = statistics.fmean(growths)
        median_growth = statistics.median(growths)
        best_pick, best_token, best_growth = max(picks, key=lambda item: item[2])
        mature_picks = [
            pick for pick, _, _ in picks
            if pick.observation_count >= MIN_FAILURE_OBSERVATIONS
            and _as_utc(pick.last_observed_at) - _as_utc(pick.first_observed_at) >= FAILURE_OBSERVATION_PERIOD
        ]
        failures = sum(
            pick.current_market_cap <= pick.reached_market_cap * 0.1
            or pick.reached_high_market_cap < pick.reached_market_cap * 1.2
            for pick in mature_picks
        )
        failure_rate = failures / len(mature_picks) * 100 if mature_picks else None
        median_component = min(100.0, max(0.0, (median_growth - 1) / 9 * 100))
        failure_component = 50.0 if failure_rate is None else 100 - failure_rate
        raw_score = (
            success_2x * 0.35
            + success_5x * 0.25
            + success_10x * 0.20
            + median_component * 0.15
            + failure_component * 0.05
        )
        sample_confidence = min(1.0, math.sqrt(total / 10))
        result[trader_id] = {
            "total_picks": total,
            "success_rate_2x": round(success_2x, 1),
            "success_rate_5x": round(success_5x, 1),
            "success_rate_10x": round(success_10x, 1),
            "average_growth": round(average_growth, 2),
            "median_growth": round(median_growth, 2),
            "best_pick": {
                "token_id": best_token.id,
                "label": best_token.ticker or best_token.name or best_token.contract_address,
                "growth_multiple": round(best_growth, 2),
            },
            "failure_rate": round(failure_rate, 1) if failure_rate is not None else None,
            "mature_pick_count": len(mature_picks),
            "performance_score": round(raw_score * sample_confidence, 1),
        }
    return result


def _qualifying_market_cap(market_cap: float | None) -> bool:
    return (
        market_cap is not None
        and math.isfinite(market_cap)
        and 0 < market_cap < PERFORMANCE_MARKET_CAP_LIMIT_USD
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _empty_performance() -> dict:
    return {
        "total_picks": 0,
        "success_rate_2x": None,
        "success_rate_5x": None,
        "success_rate_10x": None,
        "average_growth": None,
        "median_growth": None,
        "best_pick": None,
        "failure_rate": None,
        "mature_pick_count": 0,
        "performance_score": None,
    }