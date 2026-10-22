from __future__ import annotations

from typing import Any


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
            return self.keys[0]
        key = self.keys[self._index % len(self.keys)]
        self._index += 1
        return key

    def record_failure(self, key: str, cooldown_seconds: float = 0.0) -> None:
        if key:
            self._cooldowns[key] = cooldown_seconds

    def cooldown_for(self, key: str) -> float:
        return self._cooldowns.get(key, 0.0)


class HeliusClient:
    def __init__(self, api_keys: list[str] | None = None, request_timeout_seconds: int = 20, max_retries: int = 5, rotation_enabled: bool = True):
        self.rotator = HeliusKeyRotator(api_keys or [], rotation_enabled=rotation_enabled)
        self.request_timeout_seconds = request_timeout_seconds
        self.max_retries = max_retries

    def get_active_key(self) -> str | None:
        return self.rotator.get_active_key()

    def record_key_failure(self, key: str, cooldown_seconds: float = 0.0) -> None:
        self.rotator.record_failure(key, cooldown_seconds)

    def fetch_wallet_activity(self, wallet: str) -> dict[str, Any]:
        active_key = self.get_active_key()
        return {
            "wallet": wallet,
            "active_key": active_key,
            "transactions": [],
            "provider": "helius",
            "status": "ok" if active_key else "no_keys_configured",
            "remaining_keys": len(self.rotator.keys),
        }
