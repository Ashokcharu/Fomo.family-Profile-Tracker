from __future__ import annotations

from typing import Any

import httpx


DEXSCREENER_CHAIN_IDS = {
    "1": "ethereum",
    "56": "bsc",
    "8453": "base",
    "1399811149": "solana",
    "4663": "robinhood",
}


def dexscreener_chain_name(chain_name: str, network_id: str | int | None = None) -> str:
    if network_id is not None and str(network_id) in DEXSCREENER_CHAIN_IDS:
        return DEXSCREENER_CHAIN_IDS[str(network_id)]
    normalized = chain_name.strip().lower().replace(" ", "_")
    if normalized.startswith("fomo_network_"):
        network_id = normalized.removeprefix("fomo_network_")
        if network_id in DEXSCREENER_CHAIN_IDS:
            return DEXSCREENER_CHAIN_IDS[network_id]
    return {"bnb_chain": "bsc", "bnb": "bsc", "robinhood_chain": "robinhood"}.get(normalized, normalized)


class DexScreenerClient:
    def __init__(self, base_url: str | None = None):
        self.base_url = base_url or "https://api.dexscreener.com"

    def fetch_token_metrics(self, chain: str, address: str, network_id: str | int | None = None) -> dict[str, Any] | None:
        expected_chain = dexscreener_chain_name(chain, network_id)
        try:
            response = httpx.get(
                f"{self.base_url.rstrip('/')}/latest/dex/tokens/{address}",
                timeout=12,
            )
            response.raise_for_status()
            pairs = response.json().get("pairs") or []
        except (httpx.HTTPError, ValueError, AttributeError):
            return None
        return self._metrics_for_pair(pairs, address, expected_chain)

    def fetch_token_metrics_batch(self, chain: str, addresses: list[str]) -> dict[str, dict[str, Any]]:
        expected_chain = dexscreener_chain_name(chain)
        if not addresses:
            return {}
        try:
            response = httpx.get(
                f"{self.base_url.rstrip('/')}/tokens/v1/{expected_chain}/{','.join(addresses)}",
                timeout=12,
            )
            response.raise_for_status()
            pairs = response.json()
        except (httpx.HTTPError, ValueError, AttributeError):
            return {}
        if not isinstance(pairs, list):
            return {}
        return {
            address.lower(): metrics
            for address in addresses
            if (metrics := self._metrics_for_pair(pairs, address, expected_chain)) is not None
        }

    def _metrics_for_pair(self, pairs: list, address: str, expected_chain: str) -> dict[str, Any] | None:
        matching_pairs = [
            pair for pair in pairs
            if isinstance(pair, dict)
            and str((pair.get("baseToken") or {}).get("address", "")).lower() == address.lower()
            and str(pair.get("chainId", "")).lower() == expected_chain
        ]
        if not matching_pairs:
            return None
        def liquidity_usd(item: dict[str, Any]) -> float:
            try:
                return float((item.get("liquidity") or {}).get("usd") or 0)
            except (TypeError, ValueError):
                return 0

        pair = max(matching_pairs, key=liquidity_usd)
        info = pair.get("info") if isinstance(pair.get("info"), dict) else {}
        socials = info.get("socials") if isinstance(info.get("socials"), list) else []
        x_url = next((
            item.get("url") for item in socials
            if isinstance(item, dict)
            and str(item.get("type", "")).lower() in {"twitter", "x"}
            and isinstance(item.get("url"), str)
        ), None)
        liquidity = pair.get("liquidity") if isinstance(pair.get("liquidity"), dict) else {}
        volume = pair.get("volume") if isinstance(pair.get("volume"), dict) else {}
        base_token = pair.get("baseToken") if isinstance(pair.get("baseToken"), dict) else {}
        return {
            "market_cap": pair.get("marketCap") or pair.get("fdv"),
            "price": pair.get("priceUsd"),
            "liquidity": liquidity.get("usd"),
            "volume": volume.get("h24"),
            "name": base_token.get("name"),
            "symbol": base_token.get("symbol"),
            "image_url": info.get("imageUrl") or info.get("header"),
            "x_url": x_url,
            "socials": socials,
            "holder_count": pair.get("holderCount") or pair.get("holders"),
            "raw_data": pair,
            "provider_name": "dexscreener",
            "chain": expected_chain,
            "address": address,
        }
