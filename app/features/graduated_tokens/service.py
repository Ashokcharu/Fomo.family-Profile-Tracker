from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.config import get_settings
from app.features.dismissed_tokens.service import active_dismissed_token_ids
from app.features.market_data.service import (
    confirmed_market_cap_peaks,
    market_caps_are_consistent,
    preferred_market_cap_sources,
)
from app.features.tokens.service import filter_token_records, token_display_data

from app.models import DiscoveryEvent, DismissedToken, GraduationEvent, MarketObservation, RiskFinding, Token

MARKET_CAP_CONFIRMATION_WINDOW = timedelta(hours=2)


def should_graduate(
    current_market_cap: float | None,
    threshold_usd: float,
    liquidity_usd: float | None = None,
    minimum_liquidity_usd: float = 0.0,
) -> bool:
    if current_market_cap is None:
        return False
    if minimum_liquidity_usd > 0 and (
        liquidity_usd is None or liquidity_usd < minimum_liquidity_usd
    ):
        return False
    return current_market_cap >= threshold_usd


def market_cap_has_recent_confirmation(
    db: Session,
    token_id: int,
    market_cap: float | None,
    observed_at: datetime,
    market_cap_source: str | None = None,
) -> bool:
    if market_cap is None or market_cap <= 0:
        return False
    tracking_start = (
        db.query(func.coalesce(Token.first_seen_at, Token.created_at))
        .filter(Token.id == token_id)
        .scalar()
    )
    if tracking_start is None:
        return False
    previous_observation_query = db.query(MarketObservation).filter(
        MarketObservation.token_id == token_id,
        MarketObservation.is_valid.is_(True),
        MarketObservation.market_cap.is_not(None),
        MarketObservation.market_cap > 0,
        MarketObservation.observed_at >= tracking_start,
        MarketObservation.observed_at < observed_at,
        MarketObservation.observed_at >= observed_at - MARKET_CAP_CONFIRMATION_WINDOW,
    )
    if market_cap_source is not None:
        previous_observation_query = previous_observation_query.filter(
            MarketObservation.provider_name == market_cap_source
        )
    previous_observation = previous_observation_query.order_by(
        MarketObservation.observed_at.desc(),
        MarketObservation.id.desc(),
    ).first()
    if previous_observation is None:
        return False
    previous_market_cap = previous_observation.market_cap
    return market_caps_are_consistent(market_cap, previous_market_cap)


def graduation_gain_percent(discovery_market_cap: float | None, highest_observed_market_cap: float | None) -> float:
    if discovery_market_cap in (None, 0) or highest_observed_market_cap is None:
        return 0.0
    return round(((highest_observed_market_cap - discovery_market_cap) / discovery_market_cap) * 100, 2)


def list_graduated_tokens(db: Session, limit: int = 20, offset: int = 0, search: str | None = None) -> list[dict]:
    query = filter_token_records(db.query(GraduationEvent), GraduationEvent.token_id, search)
    all_rows = query.order_by(GraduationEvent.graduated_at.desc()).all()
    eligible_fomo_token_ids = _preferred_provider_eligible_token_ids(db, all_rows)
    preferred_provider_ids = set(preferred_market_cap_sources(db, [row.token_id for row in all_rows]))
    eligible_rows = [
        row for row in all_rows
        if row.token_id not in preferred_provider_ids or row.token_id in eligible_fomo_token_ids
    ]
    rows = eligible_rows[offset:offset + limit]
    token_data = token_display_data(db, [row.token_id for row in rows])
    return [
        {
            **token_data.get(row.token_id, {}),
            "id": row.id,
            "token_id": row.token_id,
            "graduated_at": row.graduated_at.isoformat(),
            "market_cap_at_graduation": row.market_cap_at_graduation,
            "threshold_usd": row.threshold_usd,
            "highest_observed_market_cap": (
                token_data.get(row.token_id, {}).get(
                    "highest_observed_market_cap",
                    row.highest_observed_market_cap,
                )
                if row.token_id in preferred_provider_ids
                else row.highest_observed_market_cap
            ),
        }
        for row in rows
    ]


def count_graduated_tokens(db: Session, search: str | None = None) -> int:
    query = filter_token_records(db.query(GraduationEvent), GraduationEvent.token_id, search)
    rows = query.all()
    eligible_fomo_token_ids = _preferred_provider_eligible_token_ids(db, rows)
    preferred_provider_ids = set(preferred_market_cap_sources(db, [row.token_id for row in rows]))
    return sum(
        row.token_id not in preferred_provider_ids or row.token_id in eligible_fomo_token_ids
        for row in rows
    )


def _preferred_provider_eligible_token_ids(
    db: Session,
    events: list[GraduationEvent],
) -> set[int]:
    token_ids = {event.token_id for event in events}
    preferred_provider_ids = set(preferred_market_cap_sources(db, token_ids))
    if not preferred_provider_ids:
        return set()

    tracking_starts = {
        token_id: first_seen_at or created_at
        for token_id, first_seen_at, created_at in db.query(
            Token.id,
            Token.first_seen_at,
            Token.created_at,
        ).filter(Token.id.in_(preferred_provider_ids)).all()
    }
    preferred_observations = db.query(
        MarketObservation.token_id,
        MarketObservation.market_cap,
    ).filter(
        MarketObservation.token_id.in_(preferred_provider_ids),
        MarketObservation.is_valid.is_(True),
        MarketObservation.market_cap.is_not(None),
        MarketObservation.market_cap > 0,
        or_(
            MarketObservation.provider_name == "fomo_family",
            MarketObservation.source == "fomo_family_discovery",
        ),
    )
    tracking_filters = [
        (MarketObservation.token_id == token_id)
        & (MarketObservation.observed_at >= tracking_start)
        for token_id, tracking_start in tracking_starts.items()
    ]
    if tracking_filters:
        preferred_observations = preferred_observations.filter(or_(*tracking_filters))
    else:
        return set()
    first_market_caps: dict[int, float] = {}
    for token_id, market_cap in preferred_observations.order_by(
        MarketObservation.observed_at,
        MarketObservation.id,
    ).all():
        first_market_caps.setdefault(token_id, market_cap)

    tracking_starts = {
        token_id: tracking_start
        for token_id, tracking_start in tracking_starts.items()
        if token_id in first_market_caps
    }
    confirmed_peaks = confirmed_market_cap_peaks(
        db,
        preferred_provider_ids,
        tracking_starts,
        {token_id: "fomo_family" for token_id in preferred_provider_ids},
    )
    thresholds = {
        event.token_id: event.threshold_usd or get_settings().graduation_threshold_usd
        for event in events
    }
    return {
        token_id
        for token_id in preferred_provider_ids
        if token_id in first_market_caps
        and first_market_caps[token_id] < thresholds.get(token_id, get_settings().graduation_threshold_usd)
        and confirmed_peaks.get(token_id, (0.0, None))[0] >= thresholds.get(
            token_id,
            get_settings().graduation_threshold_usd,
        )
    }


def reconcile_graduation_eligibility(db: Session, threshold_usd: float) -> int:
    token_rows = db.query(Token.id, Token.first_seen_at, Token.created_at).all()
    token_ids = [token_id for token_id, _, _ in token_rows]
    if not token_ids:
        return 0
    preferred_market_cap_providers = preferred_market_cap_sources(
        db,
        token_ids,
    )
    tracking_starts = {
        token_id: first_seen_at or created_at
        for token_id, first_seen_at, created_at in token_rows
    }

    ranked_observations = (
        db.query(
            MarketObservation.token_id.label("token_id"),
            MarketObservation.market_cap.label("market_cap"),
            func.row_number().over(
                partition_by=MarketObservation.token_id,
                order_by=(MarketObservation.observed_at.asc(), MarketObservation.id.asc()),
            ).label("row_number"),
        )
        .join(Token, Token.id == MarketObservation.token_id)
        .filter(
            MarketObservation.token_id.in_(token_ids),
            MarketObservation.is_valid.is_(True),
            MarketObservation.market_cap.is_not(None),
            MarketObservation.observed_at >= func.coalesce(Token.first_seen_at, Token.created_at),
        )
    )
    preferred_provider_ids = set(preferred_market_cap_providers)
    if preferred_provider_ids:
        ranked_observations = ranked_observations.filter(
            ~MarketObservation.token_id.in_(preferred_provider_ids)
            | or_(
                MarketObservation.provider_name == "fomo_family",
                MarketObservation.source == "fomo_family_discovery",
            )
        )
    ranked_observations = ranked_observations.subquery()
    rmc_by_token = {
        token_id: market_cap
        for token_id, market_cap in db.query(
            ranked_observations.c.token_id,
            ranked_observations.c.market_cap,
        ).filter(ranked_observations.c.row_number == 1).all()
    }

    missing_observation_ids = set(token_ids) - set(rmc_by_token)
    if missing_observation_ids:
        ranked_discoveries = (
            db.query(
                DiscoveryEvent.token_id.label("token_id"),
                DiscoveryEvent.initial_market_cap.label("market_cap"),
                func.row_number().over(
                    partition_by=DiscoveryEvent.token_id,
                    order_by=(DiscoveryEvent.discovered_at.asc(), DiscoveryEvent.id.asc()),
                ).label("row_number"),
            )
            .join(Token, Token.id == DiscoveryEvent.token_id)
            .filter(
                DiscoveryEvent.token_id.in_(missing_observation_ids),
                DiscoveryEvent.initial_market_cap.is_not(None),
                DiscoveryEvent.discovered_at >= func.coalesce(Token.first_seen_at, Token.created_at),
            )
        )
        if preferred_provider_ids:
            ranked_discoveries = ranked_discoveries.filter(
                ~DiscoveryEvent.token_id.in_(preferred_provider_ids)
                | (DiscoveryEvent.provider_name == "fomo_family")
            )
        ranked_discoveries = ranked_discoveries.subquery()
        rmc_by_token.update({
            token_id: market_cap
            for token_id, market_cap in db.query(
                ranked_discoveries.c.token_id,
                ranked_discoveries.c.market_cap,
            ).filter(ranked_discoveries.c.row_number == 1).all()
        })

    dismissed_ids = active_dismissed_token_ids(db)
    high_rmc_ids = {
        token_id for token_id, market_cap in rmc_by_token.items()
        if market_cap >= threshold_usd
    }
    for token_id in high_rmc_ids - dismissed_ids:
        db.add(DismissedToken(
            token_id=token_id,
            reason=f"RMC at tracking start was at or above ${threshold_usd:,.0f}",
            restored=False,
        ))
    if high_rmc_ids:
        db.query(RiskFinding).filter(RiskFinding.token_id.in_(high_rmc_ids)).delete(synchronize_session=False)

    events = db.query(GraduationEvent).all()
    candidates = []
    for event in events:
        rmc = rmc_by_token.get(event.token_id)
        tracking_start = tracking_starts.get(event.token_id)
        if (
            rmc is None
            or rmc >= threshold_usd
            or tracking_start is None
            or event.graduated_at < tracking_start
        ):
            continue
        candidates.append(event)

    tracking_start_by_candidate = {
        event.token_id: tracking_starts[event.token_id]
        for event in candidates
    }
    confirmed_peaks = confirmed_market_cap_peaks(
        db,
        tracking_start_by_candidate,
        tracking_start_by_candidate,
        preferred_market_cap_providers,
    )

    invalid_events = [
        event
        for event in events
        if event not in candidates
        or confirmed_peaks.get(event.token_id, (0.0, None))[0] < threshold_usd
    ]
    for event in invalid_events:
        token = db.query(Token).filter(Token.id == event.token_id).first()
        if token is not None:
            token.is_graduated = False
        db.delete(event)

    peak_changed = False
    for event in candidates:
        confirmed_peak = confirmed_peaks.get(event.token_id)
        peak = confirmed_peak[0] if confirmed_peak is not None else None
        peak_at = confirmed_peak[1] if confirmed_peak is not None else None
        if event.highest_observed_market_cap != peak or event.highest_observed_at != peak_at:
            event.highest_observed_market_cap = peak
            event.highest_observed_at = peak_at
            peak_changed = True
    if invalid_events or high_rmc_ids or peak_changed:
        db.commit()
    return len(invalid_events)


def create_graduation(db: Session, token: Token, threshold_usd: float, market_cap_at_graduation: float | None = None) -> GraduationEvent:
    event = GraduationEvent(
        token_id=token.id,
        market_cap_at_graduation=market_cap_at_graduation,
        threshold_usd=threshold_usd,
        highest_observed_market_cap=market_cap_at_graduation,
    )
    token.is_graduated = True
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def record_graduation_progress(
    db: Session,
    token: Token,
    threshold_usd: float,
    market_cap: float | None,
    observed_at: datetime | None = None,
    existing_event: GraduationEvent | None = None,
    liquidity_usd: float | None = None,
    minimum_liquidity_usd: float = 0.0,
    market_cap_confirmed: bool = False,
) -> GraduationEvent | None:
    if not should_graduate(
        market_cap,
        threshold_usd,
        liquidity_usd,
        minimum_liquidity_usd,
    ) and existing_event is None:
        return None
    if market_cap is None:
        return None
    if minimum_liquidity_usd > 0 and (
        liquidity_usd is None or liquidity_usd < minimum_liquidity_usd
    ):
        return None

    event = existing_event
    if event is None:
        event = (
            db.query(GraduationEvent)
            .filter(GraduationEvent.token_id == token.id)
            .order_by(GraduationEvent.graduated_at.asc(), GraduationEvent.id.asc())
            .first()
        )
    if not market_cap_confirmed:
        return event
    if event is None:
        graduated_at = observed_at or datetime.now(timezone.utc)
        event = GraduationEvent(
            token_id=token.id,
            graduated_at=graduated_at,
            market_cap_at_graduation=market_cap,
            threshold_usd=threshold_usd,
            highest_observed_market_cap=market_cap,
            highest_observed_at=graduated_at,
        )
        db.add(event)
    elif event.highest_observed_market_cap is None or market_cap > event.highest_observed_market_cap:
        event.highest_observed_market_cap = market_cap
        event.highest_observed_at = observed_at or datetime.now(timezone.utc)

    token.is_graduated = True
    return event
