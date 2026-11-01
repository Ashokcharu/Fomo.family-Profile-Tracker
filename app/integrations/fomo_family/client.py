from __future__ import annotations

from typing import Any


class FomoFamilyClient:
    """Minimal Fomo Family client wrapper with safe manual-only fallback behavior."""

    def __init__(self, base_url: str | None = None, api_token: str | None = None):
        self.base_url = base_url or "https://api.fomo.family"
        self.api_token = api_token

    def sync_followed_users(self, username: str | None = None, limit: int = 25) -> dict[str, Any]:
        normalized = (username or "").strip()
        return {
            "status": "unavailable",
            "provider": "fomo_family",
            "manual_sync_required": True,
            "username": normalized,
            "users": [],
            "message": "Fomo Family profile and following sync is not implemented; an API token alone does not enable it.",
        }

    def fetch_trader_activity(self, username: str | None = None) -> dict[str, Any]:
        return self.sync_followed_users(username=username)
