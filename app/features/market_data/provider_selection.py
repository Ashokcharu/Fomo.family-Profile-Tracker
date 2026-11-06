from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.models import MarketObservation

MARKET_CAP_RECONCILIATION_WINDOW = timedelta(days=2)
MAX_CONFIRMED_MARKET_CAP_MULTIPLE = 1.5
MARKET_CAP_QUERY_BATCH_SIZE = 200


def market_caps_are_consistent(first: float, second: float) -> bool:
    return min(first, second) > 0 and max(first, second) / min(first, second) <= (
        MAX_CONFIRMED_MARKET_CAP_MULTIPLE
    )


def confirmed_market_cap_peaks(
    db: Session,
    token_ids: set[int] | list[int],
    since_by_token: dict[int, datetime] | None = None,
    preferred_provider_by_token: dict[int, str] | None = None,
) -> dict[int, tuple[float, datetime]]:
    if not token_ids:
        return {}

    unique_ids = set(token_ids)
    query = db.query(
        MarketObservation.token_id,
        MarketObservation.market_cap,
        MarketObservation.observed_at,
        MarketObservation.provider_name,
        MarketObservation.source,
    ).filter(
        MarketObservation.is_valid.is_(True),
        MarketObservation.market_cap.is_not(None),
        MarketObservation.market_cap > 0,
    )
    observations = []
    if since_by_token:
        relevant_since = [
            (token_id, since)
            for token_id, since in since_by_token.items()
            if token_id in unique_ids
        ]
        if not relevant_since:
            return {}
        for start in range(0, len(relevant_since), MARKET_CAP_QUERY_BATCH_SIZE):
            batch = relevant_since[start:start + MARKET_CAP_QUERY_BATCH_SIZE]
            time_filters = [
                and_(
                    MarketObservation.token_id == token_id,
                    MarketObservation.observed_at >= since - MARKET_CAP_RECONCILIATION_WINDOW,
                )
                for token_id, since in batch
            ]
            observations.extend(
                query.filter(
                    MarketObservation.token_id.in_([token_id for token_id, _ in batch]),
                    or_(*time_filters),
                )
                .order_by(
                    MarketObservation.token_id,
                    MarketObservation.observed_at,
                    MarketObservation.id,
                )
                .all()
            )
    else:
        observations = (
            query.filter(MarketObservation.token_id.in_(unique_ids))
            .order_by(
                MarketObservation.token_id,
                MarketObservation.observed_at,
                MarketObservation.id,
            )
            .all()
        )

    observations_by_token: dict[int, list[tuple[float, datetime]]] = {}
    for token_id, market_cap, observed_at, provider_name, source in observations:
        if (
            preferred_provider_by_token
            and token_id in preferred_provider_by_token
            and provider_name != preferred_provider_by_token[token_id]
            and not (
                preferred_provider_by_token[token_id] == "fomo_family"
                and source == "fomo_family_discovery"
            )
        ):
            continue
        observations_by_token.setdefault(token_id, []).append((market_cap, observed_at))

    confirmed_peaks: dict[int, tuple[float, datetime]] = {}
    for token_id, observations in observations_by_token.items():
        since = since_by_token.get(token_id) if since_by_token else None
        for previous, current in zip(observations, observations[1:]):
            previous_cap, previous_at = previous
            current_cap, current_at = current
            previous_at_utc = _as_utc(previous_at)
            current_at_utc = _as_utc(current_at)
            if (
                current_at_utc < previous_at_utc
                or current_at_utc - previous_at_utc > MARKET_CAP_RECONCILIATION_WINDOW
                or not market_caps_are_consistent(previous_cap, current_cap)
            ):
                continue

            for market_cap, observed_at in (previous, current):
                if since is not None and _as_utc(observed_at) < _as_utc(since):
                    continue
                peak = confirmed_peaks.get(token_id)
                if peak is None or market_cap > peak[0]:
                    confirmed_peaks[token_id] = (market_cap, observed_at)

    return confirmed_peaks


def preferred_market_cap_sources(
    db: Session,
    token_ids: set[int] | list[int],
    *,
    fresh_after: datetime | None = None,
) -> dict[int, str]:
    if not token_ids:
        return {}
    query = db.query(MarketObservation.token_id).filter(
        MarketObservation.token_id.in_(set(token_ids)),
        MarketObservation.provider_name == "fomo_family",
        MarketObservation.market_cap.is_not(None),
        MarketObservation.market_cap > 0,
    )
    if fresh_after is not None:
        query = query.filter(
            MarketObservation.is_valid.is_(True),
            MarketObservation.observed_at >= fresh_after,
        )
    return {
        token_id: "fomo_family"
        for (token_id,) in query.distinct().all()
    }


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
