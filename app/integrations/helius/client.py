from __future__ import annotations

import time
from typing import Any

import httpx


class HeliusKeyRotator:
    def __init__(self, keys: list[str] | None = None, rotation_enabled: bool = True):
        self.keys = [key.strip() for key in (keys or []) if key and key.strip()]
        self.rotation_enabled = rotation_enabled
        self._index = 0
        self._cooldowns: dict[str, float] = {}

    def get_active_key(self) -> str | None:
        if not self.keys:
            return None

        if not self.rotation_enabled:
            key = self.keys[0]
            return key if self.cooldown_for(key) <= 0 else None

        for _ in range(len(self.keys)):
            key = self.keys[self._index % len(self.keys)]
            self._index = (self._index + 1) % len(self.keys)
            if self.cooldown_for(key) <= 0:
                return key
        return None

    def record_failure(self, key: str, cooldown_seconds: float = 0.0) -> None:
        if key:
            self._cooldowns[key] = time.monotonic() + max(float(cooldown_seconds), 0.0)

    def cooldown_for(self, key: str) -> float:
        expiry = self._cooldowns.get(key)
        if expiry is None:
            return 0.0
        remaining = expiry - time.monotonic()
        return max(0.0, remaining)


class HeliusClient:
    def __init__(self, api_keys: list[str] | None = None, request_timeout_seconds: int = 20, max_retries: int = 5, rotation_enabled: bool = True):
        self.rotator = HeliusKeyRotator(api_keys or [], rotation_enabled=rotation_enabled)
        self.request_timeout_seconds = request_timeout_seconds
        self.max_retries = max_retries
        self.base_url = "https://api.helius-rpc.com"

    def get_active_key(self) -> str | None:
        return self.rotator.get_active_key()

    def record_key_failure(self, key: str, cooldown_seconds: float = 0.0) -> None:
        self.rotator.record_failure(key, cooldown_seconds)

    def _call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        last_error: str | None = None
        for attempt in range(max(1, self.max_retries + 1)):
            active_key = self.get_active_key()
            if not active_key:
                return {
                    "provider": "helius",
                    "method": method,
                    "status": "no_keys_configured",
                    "transactions": [],
                    "accounts": [],
                    "error": "all_helius_keys_in_cooldown",
                }

            payload = {
                "jsonrpc": "2.0",
                "id": "fomo-security-analysis",
                "method": method,
                "params": params or {},
            }
            try:
                response = httpx.post(
                    f"{self.base_url}?api-key={active_key}",
                    json=payload,
                    timeout=self.request_timeout_seconds,
                )
                response.raise_for_status()
                data = response.json()
                result = data.get("result")
                if isinstance(result, dict) and "error" in result:
                    last_error = str(result.get("error"))
                    self.record_key_failure(active_key, cooldown_seconds=30.0)
                    if attempt < self.max_retries:
                        continue
                    return {"provider": "helius", "method": method, "status": "error", "error": last_error}
                return {"provider": "helius", "method": method, "status": "ok", "result": result}
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                last_error = str(exc)
                self.record_key_failure(active_key, cooldown_seconds=30.0)
                if attempt < self.max_retries:
                    continue
                return {"provider": "helius", "method": method, "status": "error", "error": last_error}

        return {"provider": "helius", "method": method, "status": "error", "error": last_error or "request_failed"}

    def fetch_wallet_activity(self, wallet: str, limit: int = 25) -> dict[str, Any]:
        response = self._call("getTokenAccounts", {"ownerAddress": wallet, "limit": limit})
        if response.get("status") != "ok":
            return {
                "wallet": wallet,
                "active_key": self.get_active_key(),
                "transactions": [],
                "provider": "helius",
                "status": response.get("status", "error"),
                "remaining_keys": len(self.rotator.keys),
                "error": response.get("error"),
            }

        accounts = response.get("result", {}).get("token_accounts") or response.get("result", {}).get("tokenAccounts") or []
        return {
            "wallet": wallet,
            "active_key": self.get_active_key(),
            "transactions": accounts,
            "provider": "helius",
            "status": "ok",
            "remaining_keys": len(self.rotator.keys),
        }

    def fetch_token_holders(self, mint: str, limit: int = 20) -> dict[str, Any]:
        response = self._call("getTokenLargestAccounts", {"mint": mint, "limit": limit})
        if response.get("status") != "ok":
            return {"provider": "helius", "token_address": mint, "status": response.get("status", "error"), "holders": [], "error": response.get("error")}

        holders = response.get("result", {}).get("value") or response.get("result", {}).get("accounts") or []
        normalized = []
        for item in holders:
            if not isinstance(item, dict):
                continue
            address = item.get("address") or item.get("owner") or item.get("wallet")
            amount = item.get("amount") or item.get("tokenAmount") or item.get("balance")
            ui_amount = item.get("uiAmount") or item.get("ui_amount")
            if not address:
                continue
            normalized.append({
                "address": address,
                "amount": amount,
                "ui_amount": ui_amount,
                "percentage_of_supply": item.get("percentage") or item.get("percent") or item.get("share") or None,
            })

        total_supply = response.get("result", {}).get("total_supply") or response.get("result", {}).get("totalSupply")
        top_5_total = 0.0
        top_10_total = 0.0
        for index, item in enumerate(normalized[:10], start=1):
            value = item.get("ui_amount")
            if value is None:
                continue
            try:
                amount = float(value)
            except (TypeError, ValueError):
                continue
            top_10_total += amount
            if index <= 5:
                top_5_total += amount

        top_5_percent = None
        top_10_percent = None
        if total_supply not in (None, 0):
            try:
                total_supply_value = float(total_supply)
                if total_supply_value:
                    if top_5_total:
                        top_5_percent = round((top_5_total / total_supply_value) * 100, 2)
                    if top_10_total:
                        top_10_percent = round((top_10_total / total_supply_value) * 100, 2)
            except (TypeError, ValueError):
                top_5_percent = None
                top_10_percent = None

        return {
            "provider": "helius",
            "token_address": mint,
            "status": "ok",
            "holders": normalized,
            "holder_count": len(normalized),
            "total_supply": total_supply,
            "top_5_percentage": top_5_percent,
            "top_10_percentage": top_10_percent,
        }

    def fetch_token_metadata(self, mint: str) -> dict[str, Any]:
        response = self._call("getAsset", {"id": mint})
        if response.get("status") != "ok":
            return {"provider": "helius", "token_address": mint, "status": response.get("status", "error"), "metadata": {}, "error": response.get("error")}

        asset = response.get("result") or {}
        content = asset.get("content") or {}
        metadata = content.get("metadata") or {}
        token_info = asset.get("token_info") or {}
        return {
            "provider": "helius",
            "token_address": mint,
            "status": "ok",
            "metadata": {
                "name": metadata.get("name") or asset.get("name"),
                "symbol": metadata.get("symbol") or asset.get("symbol"),
                "supply": token_info.get("supply") or asset.get("supply"),
                "decimals": token_info.get("decimals") or asset.get("decimals"),
                "mutable": asset.get("mutable") or token_info.get("mutable"),
                "token_program": asset.get("token_program") or token_info.get("token_program"),
                "verified": asset.get("verified_metadata") or metadata.get("verified") or False,
            },
        }

    def fetch_wallet_token_balances(self, wallet: str, limit: int = 25) -> dict[str, Any]:
        response = self._call("getTokenAccounts", {"ownerAddress": wallet, "limit": limit})
        if response.get("status") != "ok":
            return {
                "provider": "helius",
                "wallet": wallet,
                "status": response.get("status", "error"),
                "holdings": [],
                "error": response.get("error"),
            }

        raw_accounts = response.get("result", {}).get("token_accounts") or response.get("result", {}).get("tokenAccounts") or []
        holdings = []
        for item in raw_accounts:
            if not isinstance(item, dict):
                continue
            holdings.append({
                "mint": item.get("mint") or item.get("address"),
                "amount": item.get("amount") or item.get("token_amount") or item.get("balance"),
                "ui_amount": item.get("uiAmount") or item.get("ui_amount"),
                "associated_token_address": item.get("associatedTokenAddress") or item.get("associated_token_address"),
            })
        return {
            "provider": "helius",
            "wallet": wallet,
            "status": "ok",
            "holdings": holdings,
            "holding_count": len(holdings),
        }
