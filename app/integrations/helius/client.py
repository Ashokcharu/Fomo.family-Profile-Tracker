from __future__ import annotations

import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
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

    def _call(self, method: str, params: Any | None = None) -> dict[str, Any]:
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
        response = self._call("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])
        if response.get("status") != "ok":
            return {
                "provider": "helius",
                "token_address": mint,
                "status": response.get("status", "error"),
                "holders": [],
                "holder_count": None,
                "returned_account_count": 0,
                "top_5_percentage": None,
                "top_10_percentage": None,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "error": response.get("error"),
            }

        result = response.get("result")
        if not isinstance(result, dict):
            return {
                "provider": "helius",
                "token_address": mint,
                "status": "invalid_response",
                "holders": [],
                "holder_count": None,
                "returned_account_count": 0,
                "top_5_percentage": None,
                "top_10_percentage": None,
                "observed_at": datetime.now(timezone.utc).isoformat(),
            }
        holders = result.get("value") or result.get("accounts") or []
        normalized = []
        for item in holders[:max(1, min(int(limit), 20))]:
            if not isinstance(item, dict):
                continue
            address = item.get("address") or item.get("owner") or item.get("wallet")
            amount = item.get("amount") or item.get("tokenAmount") or item.get("balance")
            ui_amount = _ui_amount(item)
            if not address:
                continue
            normalized.append({
                "address": address,
                "account_address": address,
                "amount": amount,
                "ui_amount": ui_amount,
                "percentage_of_supply": None,
            })

        supply_response = self._call("getTokenSupply", [mint, {"commitment": "confirmed"}])
        supply_result = supply_response.get("result") if supply_response.get("status") == "ok" else None
        supply_value = supply_result.get("value") if isinstance(supply_result, dict) else None
        total_supply = (
            _ui_amount(supply_value)
            if isinstance(supply_value, dict)
            else _number_or_none(result.get("total_supply") or result.get("totalSupply"))
        )
        for item in normalized:
            if total_supply and item["ui_amount"] is not None:
                item["percentage_of_supply"] = round(item["ui_amount"] / total_supply * 100, 6)
        supplied_count = result.get("holder_count") or result.get("total_holder_count") or result.get("totalHolderCount")
        top_5_percent = _percentage_of_supply(normalized[:5], total_supply)
        top_10_percent = _percentage_of_supply(normalized[:10], total_supply)

        return {
            "provider": "helius",
            "token_address": mint,
            "status": "ok",
            "holders": normalized,
            "holder_count": _integer_or_none(supplied_count),
            "returned_account_count": len(normalized),
            "total_supply": total_supply,
            "top_5_percentage": top_5_percent,
            "top_10_percentage": top_10_percent,
            "concentration_status": "verified" if top_10_percent is not None else "unknown",
            "concentration_basis": "top_token_accounts_as_share_of_total_supply",
            "supply_status": "verified" if total_supply is not None else supply_response.get("status", "unavailable"),
            "observed_at": datetime.now(timezone.utc).isoformat(),
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
                "authorities": _normalized_addresses(asset.get("authorities")),
                "creators": _normalized_addresses(asset.get("creators")),
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


def _number_or_none(value: Any) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if numeric >= 0 else None


def _ui_amount(value: Any) -> float | None:
    if not isinstance(value, dict):
        return _number_or_none(value)
    ui_value = value.get("uiAmount")
    if ui_value is None:
        ui_value = value.get("ui_amount") or value.get("uiAmountString")
    if ui_value is not None:
        return _number_or_none(ui_value)
    raw_amount = value.get("amount")
    decimals = _integer_or_none(value.get("decimals"))
    if raw_amount is None or decimals is None:
        return None
    try:
        return float(Decimal(str(raw_amount)) / (Decimal(10) ** decimals))
    except (InvalidOperation, ValueError, OverflowError):
        return None


def _integer_or_none(value: Any) -> int | None:
    numeric = _number_or_none(value)
    return int(numeric) if numeric is not None else None


def _percentage_of_supply(accounts: list[dict[str, Any]], total_supply: Any) -> float | None:
    try:
        supply = Decimal(str(total_supply))
        amounts = [Decimal(str(account.get("ui_amount"))) for account in accounts]
    except (InvalidOperation, TypeError, ValueError):
        return None
    if supply <= 0 or not accounts:
        return None
    if any(amount < 0 for amount in amounts):
        return None
    return round(float(sum(amounts) / supply * 100), 2)


def _normalized_addresses(values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        return []
    normalized = []
    for item in values:
        if isinstance(item, dict) and isinstance(item.get("address"), str):
            normalized.append({
                "address": item["address"],
                "share": _number_or_none(item.get("share")),
                "verified": item.get("verified") is True,
            })
        elif isinstance(item, str):
            normalized.append({"address": item, "share": None, "verified": False})
    return normalized
