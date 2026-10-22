from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "multichain-token-intelligence"
    app_env: Literal["development", "production", "test"] = "development"
    project_name: str = "multichain-token-intelligence"
    log_level: str = "INFO"
    database_url: str = "sqlite:///./app.db"
    redis_url: str = "redis://localhost:6379/0"

    token_refresh_interval_seconds: int = 300
    discovery_threshold_usd: float = 50_000.0
    discovery_preferred_min_usd: float = 10_000.0
    discovery_preferred_max_usd: float = 30_000.0
    graduation_threshold_usd: float = 50_000.0
    minimum_unique_buyers: int = 2
    buy_cluster_window_seconds: int = 900
    minimum_liquidity_usd: float = 5_000.0
    high_priority_data_freshness_seconds: int = 60

    request_timeout_seconds: int = 15
    max_retries: int = 3
    backoff_factor: float = 2.0
    max_concurrent_jobs: int = 10
    pagination_default_limit: int = 50
    pagination_max_limit: int = 200
    ai_discovery_enabled: bool = True
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen3:8b"

    fomo_family_api_url: str = "https://api.fomo.family"
    fomo_activity_monitor_enabled: bool = False
    fomo_activity_poll_interval_seconds: int = 300
    fomo_activity_batch_size: int = 25
    fomo_activity_page_timeout_seconds: int = 20
    fomo_activity_cdp_url: str = "http://127.0.0.1:9222"
    fomo_activity_browser_profile_dir: str = "~/.cache/fomo-following"
    fomo_family_profile_handle: str = ""
    fomo_family_profile_url: str = ""
    fomo_following_sync_interval_seconds: int = 43_200
    dexscreener_api_url: str = "https://api.dexscreener.com"
    helius_api_keys: str = ""
    helius_key_rotation_enabled: bool = True
    helius_request_timeout_seconds: int = 20
    helius_max_retries: int = 5

    enabled_chains: str = "solana,robinhood_chain,base,bnb_chain,ethereum,monad"

    @field_validator("enabled_chains")
    @classmethod
    def parse_enabled_chains(cls, value):
        if not value:
            return ""
        return ",".join(item.strip() for item in value.split(",") if item.strip())

    @property
    def enabled_chain_list(self) -> list[str]:
        return [item.strip() for item in self.enabled_chains.split(",") if item.strip()]

    @property
    def helius_keys(self) -> list[str]:
        if not self.helius_api_keys:
            return []
        return [item.strip() for item in self.helius_api_keys.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
