from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, delete, func, select, union
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal
from app.database import SessionLocal
from app.features.dismissed_tokens.service import active_dismissed_token_ids
from app.features.graduated_tokens.service import record_graduation_progress
from app.features.risk_analysis.service import record_rmc_drawdown_risk
from app.features.followed_users.performance import update_token_pick_market_caps
from app.integrations.dexscreener.client import DexScreenerClient, dexscreener_chain_name
from app.models import Chain, DiscoveryEvent, DismissedToken, FollowedTrader, GraduationEvent, MarketObservation, RiskFinding, Token, TraderTransactionEvent

logger = logging.getLogger(__name__)
FALLBACK_REQUEST_LIMIT = 8
MARKET_WRITE_BATCH_SIZE = 100
MARKET_OBSERVATION_FULL_RESOLUTION_HOURS = 6
MARKET_OBSERVATION_HOURLY_RETENTION_DAYS = 7
MARKET_OBSERVATION_RETENTION_DAYS = 180
_fallback_cursor = 0


def refresh_market_observations(base_url: str) -> int:
    global _fallback_cursor

    client = DexScreenerClient(base_url)
    with SessionLocal() as db:
        token_query = (
            db.query(Token, Chain.chain_name, Chain.chain_id)
            .join(Chain, Token.chain_id == Chain.id)
            .order_by(Token.id)
        )
        dismissed_ids = active_dismissed_token_ids(db)
        if dismissed_ids:
            token_query = token_query.filter(~Token.id.in_(dismissed_ids))
        token_snapshots = [
            (token.id, token.contract_address, chain_name, network_id)
            for token, chain_name, network_id in token_query.all()
        ]

    addresses_by_chain: dict[str, list[str]] = {}
    for _, address, chain_name, network_id in token_snapshots:
        provider_chain = dexscreener_chain_name(chain_name, network_id)
        addresses_by_chain.setdefault(provider_chain, []).append(address)
    batch_requests = [
        (chain, addresses[start:start + 30])
        for chain, addresses in addresses_by_chain.items()
        for start in range(0, len(addresses), 30)
    ]
    with ThreadPoolExecutor(max_workers=8) as executor:
        batch_results = list(executor.map(lambda args: client.fetch_token_metrics_batch(*args), batch_requests))
    metrics_by_token = {
        (chain, address): metrics
        for (chain, addresses), result in zip(batch_requests, batch_results)
        for address, metrics in result.items()
    }
    missing_requests = [
        (
            (dexscreener_chain_name(chain_name, network_id), address.lower()),
            dexscreener_chain_name(chain_name, network_id),
            address,
            network_id,
        )
        for _, address, chain_name, network_id in token_snapshots
        if (dexscreener_chain_name(chain_name, network_id), address.lower()) not in metrics_by_token
    ]
    fallback_requests = missing_requests
    if len(missing_requests) > FALLBACK_REQUEST_LIMIT:
        start = _fallback_cursor % len(missing_requests)
        fallback_requests = [
            missing_requests[(start + index) % len(missing_requests)]
            for index in range(FALLBACK_REQUEST_LIMIT)
        ]
        _fallback_cursor = (start + FALLBACK_REQUEST_LIMIT) % len(missing_requests)
    else:
        _fallback_cursor = 0
    with ThreadPoolExecutor(max_workers=8) as executor:
        fallback_results = list(executor.map(
            lambda request: (request[0], client.fetch_token_metrics(request[1], request[2], request[3])),
            fallback_requests,
        ))
    metrics_by_token.update({key: metrics for key, metrics in fallback_results if metrics is not None})

    with SessionLocal() as db:
        token_ids = [token_id for token_id, _, _, _ in token_snapshots]
        if not token_ids:
            return 0
        current_dismissed_ids = active_dismissed_token_ids(db)
        token_query = (
            db.query(Token, Chain.chain_name, Chain.chain_id)
            .join(Chain, Token.chain_id == Chain.id)
            .filter(Token.id.in_(token_ids))
            .order_by(Token.id)
        )
        if current_dismissed_ids:
            token_query = token_query.filter(~Token.id.in_(current_dismissed_ids))
        tokens = token_query.all()
        graduation_events = {
            event.token_id: event
            for event in db.query(GraduationEvent).order_by(GraduationEvent.graduated_at.asc()).all()
        }
        graduation_threshold = get_settings().graduation_threshold_usd
        buyer_counts = dict(
            db.query(
                TraderTransactionEvent.token_id,
                func.count(func.distinct(TraderTransactionEvent.followed_trader_id)),
            )
            .join(FollowedTrader, TraderTransactionEvent.followed_trader_id == FollowedTrader.id)
            .filter(
                TraderTransactionEvent.token_id.in_([token.id for token, _, _ in tokens]),
                TraderTransactionEvent.event_type == "buy",
                TraderTransactionEvent.followed_trader_id.is_not(None),
            )
            .group_by(TraderTransactionEvent.token_id)
            .all()
        ) if tokens else {}
        fomo_holder_counts = _latest_fomo_holder_counts(db, [token.id for token, _, _ in tokens])
        fomo_market_metrics = _latest_fomo_market_metrics(db, [token.id for token, _, _ in tokens])

        added = 0
        for token, chain_name, network_id in tokens:
            provider_chain = dexscreener_chain_name(chain_name, network_id)
            metrics = dict(metrics_by_token.get((provider_chain, token.contract_address.lower())) or {})
            fomo_fallback_fields = []
            for field, value in fomo_market_metrics.get(token.id, {}).items():
                if metrics.get(field) is None:
                    metrics[field] = value
                    fomo_fallback_fields.append(field)
            if fomo_fallback_fields and not metrics.get("provider_name"):
                metrics["provider_name"] = "fomo_family"
            token.name = metrics.get("name") or token.name
            token.ticker = metrics.get("symbol") or token.ticker
            has_market_data = any(
                metrics.get(field) is not None
                for field in ("market_cap", "price", "liquidity", "volume")
            )
            if not has_market_data:
                continue

            reach_market_cap = _reach_market_cap(db, token.id)
            initial_rmc = reach_market_cap
            if initial_rmc is None:
                initial_rmc = metrics.get("market_cap")
            graduation_event = graduation_events.get(token.id)
            has_eligible_reach_market_cap = initial_rmc is not None and initial_rmc < graduation_threshold
            if initial_rmc is not None and initial_rmc >= graduation_threshold:
                if token.id not in current_dismissed_ids:
                    db.add(DismissedToken(
                        token_id=token.id,
                        reason=f"RMC at tracking start was at or above ${graduation_threshold:,.0f}",
                        restored=False,
                    ))
                    db.query(RiskFinding).filter(RiskFinding.token_id == token.id).delete(
                        synchronize_session=False
                    )
                    current_dismissed_ids.add(token.id)
                if graduation_event is not None:
                    db.delete(graduation_event)
                    graduation_events.pop(token.id, None)
                token.is_graduated = False
                continue
            if graduation_event is not None and not has_eligible_reach_market_cap:
                db.delete(graduation_event)
                token.is_graduated = False
                graduation_events.pop(token.id, None)
                graduation_event = None
            observed_at = datetime.now(timezone.utc)
            db.add(MarketObservation(
                token_id=token.id,
                source="scheduled_refresh",
                provider_name=metrics.get("provider_name"),
                observed_at=observed_at,
                market_cap=metrics.get("market_cap"),
                price=metrics.get("price"),
                liquidity=metrics.get("liquidity"),
                volume=metrics.get("volume"),
                is_valid=True,
                raw_data={
                    "x_url": metrics.get("x_url"),
                    "socials": metrics.get("socials"),
                    "holder_count": metrics.get("holder_count"),
                    "fomo_holder_count": fomo_holder_counts.get(token.id),
                    "buyer_count": buyer_counts.get(token.id, 0),
                    "image_url": metrics.get("image_url"),
                    "fdv": metrics.get("fdv"),
                    "market_cap_source": (
                        "fomo_family" if "market_cap" in fomo_fallback_fields
                        else metrics.get("provider_name") if metrics.get("market_cap") is not None
                        else None
                    ),
                    "fomo_fallback_fields": fomo_fallback_fields,
                },
            ))
            update_token_pick_market_caps(db, token.id, metrics.get("market_cap"), observed_at)
            if metrics.get("market_cap") is not None and (
                graduation_event is not None or has_eligible_reach_market_cap
            ):
                graduation_event = record_graduation_progress(
                    db,
                    token,
                    graduation_threshold,
                    metrics["market_cap"],
                    observed_at,
                    graduation_event,
                    liquidity_usd=metrics.get("liquidity"),
                    minimum_liquidity_usd=get_settings().minimum_liquidity_usd,
                )
                if graduation_event is not None:
                    graduation_events[token.id] = graduation_event
            record_rmc_drawdown_risk(db, token, reach_market_cap, metrics.get("market_cap"))
            added += 1
            if added % MARKET_WRITE_BATCH_SIZE == 0:
                db.commit()
        if added % MARKET_WRITE_BATCH_SIZE or db.dirty:
            db.commit()
        return added


def compact_market_observations(db: Session, now: datetime | None = None) -> int:
    """Downsample old scheduled snapshots while retaining first and peak rows."""
    now = now or datetime.now(timezone.utc)
    recent_cutoff = now - timedelta(hours=MARKET_OBSERVATION_FULL_RESOLUTION_HOURS)
    daily_cutoff = now - timedelta(days=MARKET_OBSERVATION_HOURLY_RETENTION_DAYS)
    retention_cutoff = now - timedelta(days=MARKET_OBSERVATION_RETENTION_DAYS)
    observation = MarketObservation

    if db.get_bind().dialect.name == "sqlite":
        hour_bucket = func.strftime("%Y-%m-%d %H", observation.observed_at)
        day_bucket = func.strftime("%Y-%m-%d", observation.observed_at)
    else:
        hour_bucket = func.date_trunc("hour", observation.observed_at)
        day_bucket = func.date_trunc("day", observation.observed_at)
    bucket = case(
        (observation.observed_at < daily_cutoff, day_bucket),
        else_=hour_bucket,
    )

    bucketed = (
        select(
            observation.id.label("id"),
            observation.token_id.label("token_id"),
            observation.observed_at.label("observed_at"),
            observation.market_cap.label("market_cap"),
            bucket.label("bucket"),
        )
        .where(
            observation.source == "scheduled_refresh",
            observation.observed_at >= retention_cutoff,
            observation.observed_at < recent_cutoff,
        )
        .subquery()
    )
    latest_ranked = select(
        bucketed.c.id,
        func.row_number().over(
            partition_by=(bucketed.c.token_id, bucketed.c.bucket),
            order_by=(bucketed.c.observed_at.desc(), bucketed.c.id.desc()),
        ).label("rank"),
    ).subquery()
    peak_ranked = select(
        bucketed.c.id,
        func.row_number().over(
            partition_by=(bucketed.c.token_id, bucketed.c.bucket),
            order_by=(bucketed.c.market_cap.desc(), bucketed.c.observed_at.desc(), bucketed.c.id.desc()),
        ).label("rank"),
    ).where(bucketed.c.market_cap.is_not(None)).subquery()

    all_scheduled = observation.source == "scheduled_refresh"
    first_ranked = select(
        observation.id.label("id"),
        func.row_number().over(
            partition_by=observation.token_id,
            order_by=(observation.observed_at.asc(), observation.id.asc()),
        ).label("rank"),
    ).where(all_scheduled).subquery()
    peak_ranked_all_time = select(
        observation.id.label("id"),
        func.row_number().over(
            partition_by=observation.token_id,
            order_by=(observation.market_cap.desc(), observation.observed_at.desc(), observation.id.desc()),
        ).label("rank"),
    ).where(all_scheduled, observation.market_cap.is_not(None)).subquery()

    fomo_bucketed = (
        select(
            observation.id.label("id"),
            observation.token_id.label("token_id"),
            observation.observed_at.label("observed_at"),
            day_bucket.label("bucket"),
        )
        .where(
            observation.source == "fomo_family_metadata",
            observation.observed_at >= retention_cutoff,
            observation.observed_at < daily_cutoff,
        )
        .subquery()
    )
    fomo_daily_ranked = select(
        fomo_bucketed.c.id,
        func.row_number().over(
            partition_by=(fomo_bucketed.c.token_id, fomo_bucketed.c.bucket),
            order_by=(fomo_bucketed.c.observed_at.desc(), fomo_bucketed.c.id.desc()),
        ).label("rank"),
    ).subquery()
    fomo_latest_ranked = select(
        observation.id.label("id"),
        func.row_number().over(
            partition_by=observation.token_id,
            order_by=(observation.observed_at.desc(), observation.id.desc()),
        ).label("rank"),
    ).where(observation.source == "fomo_family_metadata").subquery()
    fomo_holder_value = func.coalesce(
        observation.raw_data["fomo_holder_count"].as_string(),
        observation.raw_data["fomo_metadata"]["holder_count"].as_string(),
    )
    fomo_holder_ranked = select(
        observation.id.label("id"),
        func.row_number().over(
            partition_by=observation.token_id,
            order_by=(observation.observed_at.desc(), observation.id.desc()),
        ).label("rank"),
    ).where(
        observation.source == "fomo_family_metadata",
        fomo_holder_value.is_not(None),
    ).subquery()

    retained_ids = union(
        select(latest_ranked.c.id).where(latest_ranked.c.rank == 1),
        select(peak_ranked.c.id).where(peak_ranked.c.rank == 1),
        select(first_ranked.c.id).where(first_ranked.c.rank == 1),
        select(peak_ranked_all_time.c.id).where(peak_ranked_all_time.c.rank == 1),
        select(fomo_daily_ranked.c.id).where(fomo_daily_ranked.c.rank == 1),
        select(fomo_latest_ranked.c.id).where(fomo_latest_ranked.c.rank == 1),
        select(fomo_holder_ranked.c.id).where(fomo_holder_ranked.c.rank == 1),
    )
    result = db.execute(
        delete(observation).where(
            observation.source.in_(("scheduled_refresh", "fomo_family_metadata")),
            observation.observed_at < recent_cutoff,
            observation.id.not_in(retained_ids),
        )
    )
    db.commit()
    return result.rowcount or 0


def _latest_fomo_holder_counts(db, token_ids: list[int]) -> dict[int, int]:
    if not token_ids:
        return {}
    holder_value = func.coalesce(
        MarketObservation.raw_data["fomo_holder_count"].as_string(),
        MarketObservation.raw_data["fomo_metadata"]["holder_count"].as_string(),
    )
    ranked_observations = (
        db.query(
            MarketObservation.token_id.label("token_id"),
            holder_value.label("holder_count"),
            func.row_number().over(
                partition_by=MarketObservation.token_id,
                order_by=(MarketObservation.observed_at.desc(), MarketObservation.id.desc()),
            ).label("row_number"),
        )
        .filter(
            MarketObservation.token_id.in_(token_ids),
            holder_value.is_not(None),
        )
        .subquery()
    )
    counts = {}
    for token_id, value in db.query(
        ranked_observations.c.token_id,
        ranked_observations.c.holder_count,
    ).filter(ranked_observations.c.row_number == 1).all():
        try:
            counts[token_id] = int(value)
        except (TypeError, ValueError):
            continue
    return counts


def _latest_fomo_market_metrics(db, token_ids: list[int]) -> dict[int, dict[str, float]]:
    if not token_ids:
        return {}
    freshness_seconds = max(300, get_settings().fomo_activity_poll_interval_seconds * 2)
    fresh_after = datetime.now(timezone.utc) - timedelta(seconds=freshness_seconds)
    rows = (
        db.query(MarketObservation)
        .filter(
            MarketObservation.token_id.in_(token_ids),
            MarketObservation.source == "fomo_family_metadata",
            MarketObservation.observed_at >= fresh_after,
        )
        .order_by(MarketObservation.observed_at.desc(), MarketObservation.id.desc())
        .all()
    )
    metrics: dict[int, dict[str, float]] = {}
    for row in rows:
        token_metrics = metrics.setdefault(row.token_id, {})
        for field in ("market_cap", "price", "liquidity", "volume"):
            value = getattr(row, field)
            if field not in token_metrics and value is not None and value > 0:
                token_metrics[field] = value
    return metrics


def _reach_market_cap(db, token_id: int) -> float | None:
    tracking_start = (
        db.query(func.coalesce(Token.first_seen_at, Token.created_at))
        .filter(Token.id == token_id)
        .scalar()
    )
    if tracking_start is None:
        return None
    first_observation = (
        db.query(MarketObservation.market_cap)
        .filter(
            MarketObservation.token_id == token_id,
            MarketObservation.is_valid.is_(True),
            MarketObservation.market_cap.is_not(None),
            MarketObservation.observed_at >= tracking_start,
        )
        .order_by(MarketObservation.observed_at.asc(), MarketObservation.id.asc())
        .first()
    )
    if first_observation is not None:
        return first_observation[0]
    first_discovery = (
        db.query(DiscoveryEvent.initial_market_cap)
        .filter(
            DiscoveryEvent.token_id == token_id,
            DiscoveryEvent.initial_market_cap.is_not(None),
            DiscoveryEvent.discovered_at >= tracking_start,
        )
        .order_by(DiscoveryEvent.discovered_at.asc(), DiscoveryEvent.id.asc())
        .first()
    )
    return first_discovery[0] if first_discovery is not None else None


async def run_market_refresh(refresh_interval_seconds: int, base_url: str) -> None:
    interval = max(1, int(refresh_interval_seconds))
    last_compaction = asyncio.get_running_loop().time()
    while True:
        loop = asyncio.get_running_loop()
        if loop.time() - last_compaction >= 24 * 60 * 60:
            try:
                removed = await asyncio.to_thread(_compact_market_observations)
                logger.info("Compacted market history; removed %d redundant snapshots", removed)
            except Exception:
                logger.exception("Scheduled market history compaction failed")
            last_compaction = loop.time()
        try:
            updated = await asyncio.to_thread(refresh_market_observations, base_url)
            logger.info("Stored periodic snapshots for %d tracked tokens", updated)
        except Exception:
            logger.exception("Scheduled market refresh failed")
        await asyncio.sleep(interval)


def compact_market_history() -> int:
    with SessionLocal() as db:
        return compact_market_observations(db)
