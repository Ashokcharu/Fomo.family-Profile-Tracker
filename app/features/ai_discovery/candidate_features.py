from __future__ import annotations

import math


def build_candidate_features(token: dict, settings) -> dict:
    market_cap = _number(token.get("market_cap"))
    liquidity = _number(token.get("liquidity"))
    growth = _number(token.get("growth_multiple"))
    buyers = max(0, int(_number(token.get("buyer_count")) or 0))
    holders = max(0, int(_number(token.get("fomo_holder_count")) or 0))

    preferred_min = settings.discovery_preferred_min_usd
    preferred_max = settings.discovery_preferred_max_usd
    if market_cap is None:
        market_cap_score = 0.0
    elif preferred_min <= market_cap <= preferred_max:
        market_cap_score = 25.0
    elif 0 < market_cap < settings.discovery_threshold_usd:
        market_cap_score = 15.0
    else:
        market_cap_score = 0.0

    liquidity_score = min(25.0, liquidity / settings.minimum_liquidity_usd * 25) if liquidity is not None else 0.0
    buyer_score = min(25.0, buyers / 5 * 25)
    growth_score = min(15.0, max(0.0, 5 + math.log2(growth) * 5)) if growth is not None and growth > 0 else 0.0
    holder_score = min(10.0, holders / 100 * 10) if token.get("fomo_holder_count") is not None else 0.0
    score_breakdown = {
        "market_cap_fit": round(market_cap_score, 1),
        "liquidity": round(liquidity_score, 1),
        "followed_buyers": round(buyer_score, 1),
        "observed_growth": round(growth_score, 1),
        "holders": round(holder_score, 1),
    }

    strengths = []
    risks = []
    if market_cap is None:
        risks.append("Market-cap data is unavailable.")
    elif market_cap >= settings.discovery_threshold_usd:
        risks.append("Market cap is at or above the discovery threshold.")
    elif preferred_min <= market_cap <= preferred_max:
        strengths.append("Market cap is within the preferred discovery range.")
    if liquidity is None:
        risks.append("Liquidity data is unavailable.")
    elif liquidity >= settings.minimum_liquidity_usd:
        strengths.append("Liquidity meets the configured minimum.")
    else:
        risks.append("Liquidity is below the configured minimum.")
    if buyers >= 2:
        strengths.append(f"{buyers} followed traders have bought this token.")
    elif buyers == 0:
        risks.append("No followed-trader buys are recorded.")
    else:
        strengths.append("One followed trader has bought this token.")
    if growth is not None and growth >= 2:
        strengths.append(f"Observed growth is {growth:.2f}× from the tracked baseline.")
    elif growth is not None and growth < 1:
        risks.append("Current market cap is below its tracked baseline.")
    if token.get("fomo_holder_count") is None:
        risks.append("Holder count is unavailable.")
    elif holders < 25:
        risks.append("Holder count is low.")
    if not token.get("social_links"):
        risks.append("No social links are available in tracked market data.")

    return {
        "token_id": token["id"],
        "name": token.get("name"),
        "ticker": token.get("ticker"),
        "chain_name": token.get("chain_name"),
        "contract_address": token.get("contract_address"),
        "fomo_url": token.get("fomo_url"),
        "market_cap": market_cap,
        "liquidity": liquidity,
        "buyer_count": buyers,
        "fomo_holder_count": holders if token.get("fomo_holder_count") is not None else None,
        "growth_multiple": growth,
        "growth_percent": _number(token.get("growth_percent")),
        "heuristic_score": round(sum(score_breakdown.values()), 1),
        "score_breakdown": score_breakdown,
        "strengths": strengths,
        "risks": risks,
    }


def _number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None
