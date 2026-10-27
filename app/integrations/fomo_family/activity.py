from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.chains import chain_registry
from app.features.tokens.service import token_identity_key
from app.features.followed_users.performance import record_trader_token_pick
from app.integrations.dexscreener.client import DexScreenerClient, dexscreener_chain_name
from app.models import Chain, FollowedTrader, MarketObservation, Token, TraderTransactionEvent


def parse_profile_identity(payload: dict[str, Any], expected_username: str) -> dict[str, str] | None:
    user = payload.get("responseObject")
    if not isinstance(user, dict):
        return None

    user_id = user.get("id")
    username = user.get("userHandle")
    wallet_address = user.get("address")
    if not isinstance(user_id, str) or not user_id.strip():
        return None
    if not isinstance(username, str) or username.casefold() != expected_username.casefold():
        return None

    return {
        "profile_id": user_id,
        "username": username,
        "wallet_address": wallet_address if isinstance(wallet_address, str) else "unknown",
    }


def parse_buy_swap(swap: dict[str, Any]) -> dict[str, Any] | None:
    display = swap.get("display")
    if not isinstance(display, dict) or str(display.get("side", "")).casefold() != "buy":
        return None

    swap_id = swap.get("id")
    token_address = swap.get("outTokenAddress")
    network_id = swap.get("outNetworkId") or swap.get("networkId")
    if not isinstance(swap_id, str) or not swap_id.strip():
        return None
    if not isinstance(token_address, str) or not token_address.strip() or network_id is None:
        return None

    raw_timestamp = swap.get("createdAt")
    try:
        event_time = datetime.fromisoformat(str(raw_timestamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None

    return {
        "swap_id": swap_id,
        "token_address": token_address,
        "network_id": str(network_id),
        "token_amount": _as_float(swap.get("outHumanAmount")),
        "quote_amount": _as_float(swap.get("humanUsdAmountIn")),
        "estimated_usd": _as_float(swap.get("humanUsdAmountIn")),
        "price": _as_float(swap.get("displayPriceUsd")),
        "market_cap": _swap_market_cap(swap, display),
        "event_time": event_time,
    }


def fetch_fomo_buy_market_caps(
    swaps: list[dict[str, Any]],
    base_url: str | None = None,
    token_supplies: dict[tuple[str, str], float] | None = None,
) -> dict[tuple[str, str], float]:
    parsed_swaps = [parsed for swap in swaps if (parsed := parse_buy_swap(swap)) is not None]
    market_caps: dict[tuple[str, str], float] = {}
    addresses_by_network: dict[str, dict[str, str]] = {}
    for parsed in parsed_swaps:
        network_id = parsed["network_id"]
        address = parsed["token_address"]
        key = (network_id, address.lower())
        market_cap = parsed["market_cap"]
        if market_cap is not None:
            market_caps[key] = market_cap
        else:
            circulating_supply = token_supplies.get(key) if token_supplies else None
            if circulating_supply is not None and parsed["price"] is not None:
                derived_market_cap = _as_float(circulating_supply * parsed["price"])
                if derived_market_cap is not None:
                    market_caps[key] = derived_market_cap
                    continue
            addresses_by_network.setdefault(network_id, {})[address.lower()] = address

    client = DexScreenerClient(base_url)
    for network_id, addresses in addresses_by_network.items():
        provider_chain = dexscreener_chain_name(f"Fomo network {network_id}", network_id)
        address_values = list(addresses.values())
        for start in range(0, len(address_values), 30):
            batch = address_values[start:start + 30]
            metrics = client.fetch_token_metrics_batch(provider_chain, batch)
            for address in batch:
                market_cap = _as_float((metrics.get(address.lower()) or {}).get("market_cap"))
                if market_cap is not None:
                    market_caps[(network_id, address.lower())] = market_cap
    return market_caps


def persist_fomo_buys(
    db: Session,
    trader: FollowedTrader,
    wallet_address: str,
    swaps: list[dict[str, Any]],
    *,
    discovery_threshold_usd: float | None = None,
    market_caps: dict[tuple[str, str], float] | None = None,
    token_metadata: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> int:
    inserted = 0
    for swap in swaps:
        parsed = parse_buy_swap(swap)
        if parsed is None:
            continue

        dedup_key = f"fomo:{parsed['swap_id']}"
        if db.query(TraderTransactionEvent.id).filter(TraderTransactionEvent.dedup_key == dedup_key).first():
            continue

        network_id = parsed["network_id"]
        contract_address = parsed["token_address"]
        market_cap = parsed["market_cap"]
        if market_cap is None and market_caps is not None:
            market_cap = market_caps.get((network_id, contract_address.lower()))
        if discovery_threshold_usd is not None and (
            market_cap is None
            or not math.isfinite(market_cap)
            or market_cap >= discovery_threshold_usd
        ):
            continue

        chain = db.query(Chain).filter(Chain.chain_id == network_id).first()
        if chain is None:
            chain_slug = f"fomo_network_{network_id}"
            chain = db.query(Chain).filter(Chain.chain_slug == chain_slug).first()
            if chain is None:
                chain = Chain(
                    chain_name=f"Fomo network {network_id}",
                    chain_slug=chain_slug,
                    chain_id=network_id,
                )
                db.add(chain)
                db.flush()

        chain_name, normalized_address = token_identity_key(chain.chain_slug, contract_address)
        adapter = chain_registry.get(chain_name)
        if adapter is not None:
            normalized_address = adapter.normalize_address(contract_address)
        token = (
            db.query(Token)
            .filter(Token.chain_id == chain.id, Token.normalized_address == normalized_address)
            .first()
        )
        metadata = (token_metadata or {}).get((network_id, contract_address.lower()), {})
        if token is None:
            token = Token(
                chain_id=chain.id,
                contract_address=contract_address,
                normalized_address=normalized_address,
                name=metadata.get("name"),
                ticker=metadata.get("symbol"),
                first_seen_at=datetime.now(timezone.utc),
            )
            db.add(token)
            db.flush()
        else:
            token.name = metadata.get("name") or token.name
            token.ticker = metadata.get("symbol") or token.ticker
            token.first_seen_at = token.first_seen_at or datetime.now(timezone.utc)

        if discovery_threshold_usd is not None and market_cap is not None:
            observation_data = {"fomo_metadata": metadata}
            if metadata.get("holder_count") is not None:
                observation_data["fomo_holder_count"] = metadata["holder_count"]
            if metadata.get("image_url"):
                observation_data["image_url"] = metadata["image_url"]
            db.add(MarketObservation(
                token_id=token.id,
                source="fomo_family_discovery",
                provider_name=None,
                observed_at=parsed["event_time"],
                market_cap=market_cap,
                raw_data=observation_data,
            ))
        db.add(
            TraderTransactionEvent(
                token_id=token.id,
                followed_trader_id=trader.id,
                wallet_address=wallet_address or "unknown",
                chain_id=chain.id,
                event_type="buy",
                event_time=parsed["event_time"],
                token_amount=parsed["token_amount"],
                quote_amount=parsed["quote_amount"],
                quote_denomination="USD",
                estimated_usd=parsed["estimated_usd"],
                market_cap_at_event=market_cap,
                price_at_event=parsed["price"],
                source="fomo_family",
                data_quality="provider",
                dedup_key=dedup_key,
            )
        )
        record_trader_token_pick(db, trader.id, token.id, market_cap, parsed["event_time"])
        inserted += 1

    return inserted


def persist_fomo_token_metadata(
    db: Session,
    token_metadata: dict[tuple[str, str], dict[str, Any]],
) -> None:
    for (network_id, address), metadata in token_metadata.items():
        chain = db.query(Chain).filter(Chain.chain_id == network_id).first()
        if chain is None:
            continue
        _, normalized_address = token_identity_key(chain.chain_slug, address)
        token = (
            db.query(Token)
            .filter(Token.chain_id == chain.id, Token.normalized_address == normalized_address)
            .first()
        )
        if token is None:
            continue
        token.name = metadata.get("name") or token.name
        token.ticker = metadata.get("symbol") or token.ticker
        token.first_seen_at = token.first_seen_at or datetime.now(timezone.utc)
        raw_data = {"fomo_metadata": metadata}
        if metadata.get("holder_count") is not None:
            raw_data["fomo_holder_count"] = metadata["holder_count"]
        if metadata.get("image_url"):
            raw_data["image_url"] = metadata["image_url"]
        quote_fields = ("market_cap", "price", "liquidity", "volume")
        quote_values = {field: metadata.get(field) for field in quote_fields}
        has_quote_data = any(value is not None for value in quote_values.values())
        db.add(MarketObservation(
            token_id=token.id,
            source="fomo_family_metadata",
            provider_name="fomo_family" if has_quote_data else None,
            observed_at=datetime.now(timezone.utc),
            **quote_values,
            is_valid=False,
            raw_data=raw_data,
        ))


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed_value = float(value)
        return parsed_value if math.isfinite(parsed_value) else None
    except (TypeError, ValueError):
        return None


def _swap_market_cap(swap: dict[str, Any], display: dict[str, Any]) -> float | None:
    sources = [swap, display]
    sources.extend(
        value for value in (swap.get("outToken"), swap.get("token"))
        if isinstance(value, dict)
    )
    for source in sources:
        for key in ("marketCapUsd", "market_cap_usd", "marketCap", "market_cap"):
            if source.get(key) is not None:
                return _as_float(source[key])
    return None