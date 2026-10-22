from __future__ import annotations

from app.features.tokens.service import token_display_data
from sqlalchemy.orm import Session

from app.models import MarketObservation, Token

PROVIDER_PRIORITY = ["fomo_family", "dexscreener", "helius"]


def provider_priority_for_field(field_name: str, provider_map: dict[str, dict] | None = None) -> str | None:
    provider_map = provider_map or {}
    field_map = provider_map.get(field_name, {}) if isinstance(provider_map.get(field_name), dict) else provider_map
    for provider in PROVIDER_PRIORITY:
        provider_state = field_map.get(provider, {})
        if provider_state.get("supported") is True:
            return provider
    return None


def market_cap_for_field(field_name: str, provider_map: dict[str, dict] | None = None, default=None):
    provider_map = provider_map or {}
    field_map = provider_map.get(field_name, {}) if isinstance(provider_map.get(field_name), dict) else provider_map
    provider = provider_priority_for_field(field_name, provider_map)
    if provider is None:
        return default
    return field_map.get(provider, {}).get("value", default)


def list_market_history(db: Session, limit: int = 20) -> list[dict]:
    rows = db.query(MarketObservation).order_by(MarketObservation.observed_at.desc()).limit(limit).all()
    token_data = token_display_data(db, [row.token_id for row in rows])
    return [
        {
            **token_data.get(row.token_id, {}),
            "id": row.id,
            "token_id": row.token_id,
            "source": row.source,
            "provider_name": row.provider_name,
            "market_cap": row.market_cap,
            "price": row.price,
            "liquidity": row.liquidity,
            "volume": row.volume,
            "observed_at": row.observed_at.isoformat(),
            "buyer_count": (row.raw_data or {}).get("buyer_count") if isinstance(row.raw_data, dict) else None,
            "fomo_holder_count": (row.raw_data or {}).get("fomo_holder_count") if isinstance(row.raw_data, dict) else None,
        }
        for row in rows
    ]


def upsert_market_observation(db: Session, token: Token, source: str, provider_name: str | None, market_cap: float | None, price: float | None = None, liquidity: float | None = None, volume: float | None = None) -> MarketObservation:
    observation = MarketObservation(
        token_id=token.id,
        source=source,
        provider_name=provider_name,
        market_cap=market_cap,
        price=price,
        liquidity=liquidity,
        volume=volume,
        is_valid=market_cap is not None or price is not None or liquidity is not None or volume is not None,
    )
    db.add(observation)
    db.commit()
    db.refresh(observation)
    return observation
