from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import MarketObservation, SecurityAnalysis

RISK_FACTORS = {
    "market_data_freshness": 20.0,
    "liquidity_level": 25.0,
    "liquidity_trend": 30.0,
    "market_cap_liquidity_divergence": 25.0,
    "top_token_account_concentration": 20.0,
}
COMPLETENESS_FIELDS = (
    "market_cap",
    "price",
    "liquidity",
    "volume",
    "holder_count",
    "top_token_account_concentration",
)


def build_risk_assessments(
    db: Session,
    token_ids: list[int],
    settings: Settings,
    now: datetime | None = None,
) -> dict[int, dict[str, Any]]:
    if not token_ids:
        return {}

    now = now or datetime.now(timezone.utc)
    history_start = now - timedelta(hours=12)
    latest_observation_ids = (
        db.query(
            MarketObservation.id.label("id"),
            func.row_number().over(
                partition_by=MarketObservation.token_id,
                order_by=(MarketObservation.observed_at.desc(), MarketObservation.id.desc()),
            ).label("position"),
        )
        .filter(
            MarketObservation.token_id.in_(token_ids),
            MarketObservation.is_valid.is_(True),
        )
        .subquery()
    )
    latest_observations = (
        db.query(MarketObservation)
        .join(latest_observation_ids, latest_observation_ids.c.id == MarketObservation.id)
        .filter(latest_observation_ids.c.position == 1)
        .all()
    )
    observations = (
        db.query(MarketObservation)
        .filter(
            MarketObservation.token_id.in_(token_ids),
            MarketObservation.is_valid.is_(True),
            MarketObservation.observed_at >= history_start,
        )
        .order_by(MarketObservation.token_id, MarketObservation.observed_at, MarketObservation.id)
        .all()
    )
    observations_by_token: dict[int, list[MarketObservation]] = {}
    for observation in observations:
        observations_by_token.setdefault(observation.token_id, []).append(observation)
    for observation in latest_observations:
        rows = observations_by_token.setdefault(observation.token_id, [])
        if not rows or rows[-1].id != observation.id:
            rows.append(observation)
            rows.sort(key=lambda row: (_aware_utc(row.observed_at), row.id))
    analysis_rows = (
        db.query(SecurityAnalysis)
        .filter(SecurityAnalysis.token_id.in_(token_ids))
        .order_by(SecurityAnalysis.token_id, SecurityAnalysis.id.desc())
        .all()
    )
    analyses: dict[int, SecurityAnalysis] = {}
    for analysis in analysis_rows:
        analyses.setdefault(analysis.token_id, analysis)

    stale_after_seconds = max(
        settings.token_refresh_interval_seconds * 2,
        settings.high_priority_data_freshness_seconds,
    )
    reference_before = now - timedelta(hours=1)
    assessments = {}
    for token_id in token_ids:
        rows = observations_by_token.get(token_id, [])
        latest = rows[-1] if rows else None
        reference = next(
            (
                row for row in reversed(rows)
                if latest is not None
                and row.id != latest.id
                and _aware_utc(row.observed_at) <= reference_before
            ),
            None,
        )
        assessments[token_id] = _build_assessment(
            latest,
            reference,
            now,
            stale_after_seconds,
            settings.minimum_liquidity_usd,
            analyses.get(token_id),
            settings.security_analysis_cache_ttl_seconds,
        )
    return assessments


def _build_assessment(
    latest: MarketObservation | None,
    reference: MarketObservation | None,
    now: datetime,
    stale_after_seconds: int,
    minimum_liquidity: float,
    analysis: SecurityAnalysis | None,
    concentration_freshness_seconds: int,
) -> dict[str, Any]:
    latest_at = _aware_utc(latest.observed_at) if latest is not None else None
    latest_source = (
        latest.provider_name or latest.source
        if latest is not None
        else None
    )
    raw_data = latest.raw_data if latest is not None and isinstance(latest.raw_data, dict) else {}
    holder_count = _first_number(
        raw_data.get("fomo_holder_count"),
        raw_data.get("holder_count"),
        raw_data.get("holders_count"),
        raw_data.get("holderCount"),
        raw_data.get("holders"),
    )
    metric_values = {
        "market_cap": latest.market_cap if latest is not None else None,
        "price": latest.price if latest is not None else None,
        "liquidity": latest.liquidity if latest is not None else None,
        "volume": latest.volume if latest is not None else None,
        "holder_count": holder_count,
        "top_token_account_concentration": None,
    }
    known_metrics = [field for field in COMPLETENESS_FIELDS if metric_values[field] is not None]
    reference_known = reference is not None and latest is not None
    comparison_known = (
        reference_known
        and reference.market_cap is not None
        and reference.liquidity is not None
        and latest.market_cap is not None
        and latest.liquidity is not None
        and reference.market_cap > 0
        and reference.liquidity > 0
    )
    if comparison_known:
        known_metrics.append("historical_comparison")

    factors = []
    if latest is None:
        factors = [
            _factor(name, "unknown", weight, None, "No valid market observation is available.", None, None, 0)
            for name, weight in RISK_FACTORS.items()
            if name != "top_token_account_concentration"
        ]
    else:
        age_seconds = max(0.0, (now - latest_at).total_seconds()) if latest_at else None
        freshness_status = "risk" if age_seconds is not None and age_seconds > stale_after_seconds else "clear"
        factors.append(_factor(
            "market_data_freshness",
            freshness_status,
            RISK_FACTORS["market_data_freshness"],
            100.0 if freshness_status == "risk" else 0.0,
            (
                f"Latest market observation is {age_seconds:.0f}s old; "
                f"freshness limit is {stale_after_seconds}s."
            ) if age_seconds is not None else "Observation timestamp is unavailable.",
            latest_source,
            latest_at,
            0.9,
        ))

        liquidity = latest.liquidity
        if liquidity is None or liquidity < 0:
            factors.append(_factor(
                "liquidity_level", "unknown", RISK_FACTORS["liquidity_level"], None,
                "Liquidity value is unavailable or invalid.", latest_source, latest_at, 0,
            ))
        else:
            low_liquidity = liquidity < minimum_liquidity
            factors.append(_factor(
                "liquidity_level",
                "risk" if low_liquidity else "clear",
                RISK_FACTORS["liquidity_level"],
                70.0 if low_liquidity else 0.0,
                f"Liquidity is ${liquidity:,.2f}; configured minimum is ${minimum_liquidity:,.2f}.",
                latest_source,
                latest_at,
                0.9,
            ))

        liquidity_change = None
        market_cap_change = None
        if comparison_known:
            liquidity_change = (latest.liquidity / reference.liquidity - 1) * 100
            market_cap_change = (latest.market_cap / reference.market_cap - 1) * 100

        if liquidity_change is None:
            factors.append(_factor(
                "liquidity_trend", "unknown", RISK_FACTORS["liquidity_trend"], None,
                "A valid liquidity comparison at least one hour apart is unavailable.",
                latest_source, latest_at, 0,
            ))
            factors.append(_factor(
                "market_cap_liquidity_divergence", "unknown",
                RISK_FACTORS["market_cap_liquidity_divergence"], None,
                "Market-cap and liquidity history is insufficient for comparison.",
                latest_source, latest_at, 0,
            ))
        else:
            liquidity_drop = max(0.0, -liquidity_change)
            trend_status = "risk" if liquidity_drop >= 20 else "clear"
            factors.append(_factor(
                "liquidity_trend",
                trend_status,
                RISK_FACTORS["liquidity_trend"],
                min(100.0, liquidity_drop * 2),
                (
                    f"Liquidity changed {liquidity_change:+.1f}% over "
                    f"{(_aware_utc(latest.observed_at) - _aware_utc(reference.observed_at)).total_seconds() / 3600:.1f}h."
                ),
                latest_source,
                latest_at,
                0.8,
            ))
            divergence = liquidity_change <= -20 and market_cap_change >= -10
            factors.append(_factor(
                "market_cap_liquidity_divergence",
                "risk" if divergence else "clear",
                RISK_FACTORS["market_cap_liquidity_divergence"],
                80.0 if divergence else 0.0,
                (
                    f"Market cap changed {market_cap_change:+.1f}% while liquidity changed "
                    f"{liquidity_change:+.1f}% over the comparison window."
                ),
                latest_source,
                latest_at,
                0.75,
            ))

    concentration, concentration_at = _verified_token_account_concentration(
        analysis,
        now,
        concentration_freshness_seconds,
    )
    if concentration is None:
        factors.append(_factor(
            "top_token_account_concentration",
            "unknown",
            RISK_FACTORS["top_token_account_concentration"],
            None,
            "Fresh, verified top-token-account concentration is unavailable; creator or unique-wallet linkage is not inferred.",
            None,
            concentration_at,
            0,
        ))
    else:
        threshold = 10.0
        concentration_score = min(100.0, concentration / threshold * 50)
        factors.append(_factor(
            "top_token_account_concentration",
            "risk" if concentration >= threshold else "clear",
            RISK_FACTORS["top_token_account_concentration"],
            concentration_score,
            f"Top 10 Solana token accounts hold {concentration:.2f}% of supply; accounts are not necessarily unique wallets.",
            "helius",
            concentration_at,
            0.9,
        ))
        known_metrics.append("top_token_account_concentration")

    known_factors = [factor for factor in factors if factor["status"] != "unknown"]
    known_weight = sum(factor["weight"] for factor in known_factors)
    risk_score = (
        round(sum(factor["score"] * factor["weight"] for factor in known_factors) / known_weight, 1)
        if known_weight
        else None
    )
    risk_coverage = round(known_weight / sum(RISK_FACTORS.values()) * 100, 1)
    data_completeness = round(len(known_metrics) / (len(COMPLETENESS_FIELDS) + 1) * 100, 1)

    return {
        "risk_score": risk_score,
        "risk_level": (
            "high" if risk_score is not None and risk_score >= 60
            else "moderate" if risk_score is not None and risk_score >= 30
            else "low" if risk_score is not None
            else "unknown"
        ),
        "risk_coverage_percent": risk_coverage,
        "data_completeness_percent": data_completeness,
        "data_completeness_known": known_metrics,
        "data_completeness_missing": [
            field for field in (*COMPLETENESS_FIELDS, "historical_comparison")
            if field not in known_metrics
        ],
        "data_source": latest_source,
        "observed_at": latest_at.isoformat() if latest_at else None,
        "market_cap": metric_values["market_cap"],
        "price": metric_values["price"],
        "liquidity": metric_values["liquidity"],
        "volume": metric_values["volume"],
        "confidence": (
            round(sum(factor["confidence"] * factor["weight"] for factor in known_factors) / known_weight, 2)
            if known_weight
            else None
        ),
        "factors": factors + [
            _factor(
                "holder_concentration",
                "unknown",
                0,
                None,
                "Verified holder-concentration data is not available from the current discovery data source.",
                None,
                None,
                0,
            )
        ],
    }


def _verified_token_account_concentration(
    analysis: SecurityAnalysis | None,
    now: datetime,
    freshness_seconds: int,
) -> tuple[float | None, datetime | None]:
    if analysis is None or analysis.analysis_status != "ready":
        return None, None
    payload = analysis.analysis_payload if isinstance(analysis.analysis_payload, dict) else {}
    concentration = payload.get("holder_concentration")
    if not isinstance(concentration, dict):
        return None, None
    if (
        concentration.get("concentration_provider") != "helius"
        or concentration.get("concentration_basis") != "top_token_accounts_as_share_of_total_supply"
        or concentration.get("status") != "verified"
    ):
        return None, None
    observed_value = concentration.get("concentration_observed_at")
    if not isinstance(observed_value, str):
        return None, None
    try:
        observed_at = datetime.fromisoformat(observed_value)
    except ValueError:
        return None, None
    observed_at = _aware_utc(observed_at)
    age_seconds = (now - observed_at).total_seconds()
    if age_seconds < 0 or age_seconds > max(300, freshness_seconds):
        return None, observed_at
    try:
        percentage = float(concentration.get("top_10_percent"))
    except (TypeError, ValueError):
        return None, observed_at
    if not 0 <= percentage <= 100:
        return None, observed_at
    return percentage, observed_at


def _factor(
    name: str,
    status: str,
    weight: float,
    score: float | None,
    evidence: str,
    source: str | None,
    observed_at: datetime | None,
    confidence: float,
) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "weight": weight,
        "score": round(score, 1) if score is not None else None,
        "evidence": evidence,
        "source": source,
        "observed_at": observed_at.isoformat() if observed_at else None,
        "confidence": confidence,
    }


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _first_number(*values: Any) -> float | None:
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number >= 0:
            return number
    return None
