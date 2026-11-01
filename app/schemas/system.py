from __future__ import annotations

from pydantic import BaseModel


class ConfigResponse(BaseModel):
    token_refresh_interval_seconds: int
    discovery_threshold_usd: float
    discovery_preferred_min_usd: float
    discovery_preferred_max_usd: float
    graduation_threshold_usd: float
    ai_discovery_enabled: bool
    enabled_chains: list[str]
