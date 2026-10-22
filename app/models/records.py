from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Chain(Base):
    __tablename__ = "chains"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    chain_name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    chain_slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    chain_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rpc_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    tokens: Mapped[list["Token"]] = relationship(back_populates="chain")


class Token(Base):
    __tablename__ = "tokens"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    chain_id: Mapped[int] = mapped_column(ForeignKey("chains.id"), nullable=False, index=True)
    contract_address: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_address: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    ticker: Mapped[str | None] = mapped_column(String(32), nullable=True)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    decimals: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_graduated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    chain: Mapped[Chain] = relationship(back_populates="tokens")
    market_observations: Mapped[list["MarketObservation"]] = relationship(back_populates="token")
    discovery_events: Mapped[list["DiscoveryEvent"]] = relationship(back_populates="token")
    trader_tx_events: Mapped[list["TraderTransactionEvent"]] = relationship(back_populates="token")
    graduation_events: Mapped[list["GraduationEvent"]] = relationship(back_populates="token")
    favorites: Mapped[list["Favorite"]] = relationship(back_populates="token")
    risk_findings: Mapped[list["RiskFinding"]] = relationship(back_populates="token")
    ai_records: Mapped[list["AIIntelligenceRecord"]] = relationship(back_populates="token")
    dismissed_tokens: Mapped[list["DismissedToken"]] = relationship(back_populates="token")

    __table_args__ = (
        UniqueConstraint("chain_id", "normalized_address", name="uq_chain_token_identity"),
    )


class DiscoveryEvent(Base):
    __tablename__ = "discovery_events"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id"), nullable=False, index=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    initial_market_cap: Mapped[float | None] = mapped_column(Float, nullable=True)
    initial_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    initial_liquidity: Mapped[float | None] = mapped_column(Float, nullable=True)
    initial_volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    initial_trader_activity: Mapped[JSON | None] = mapped_column(JSON, nullable=True)
    event_metadata: Mapped[JSON | None] = mapped_column(JSON, nullable=True)

    token: Mapped[Token] = relationship(back_populates="discovery_events")


class MarketObservation(Base):
    __tablename__ = "market_observations"
    __table_args__ = (Index("ix_market_observations_token_observed", "token_id", "observed_at", "id"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id"), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    market_cap: Mapped[float | None] = mapped_column(Float, nullable=True)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    liquidity: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    freshness_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_valid: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_zero_value: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    raw_data: Mapped[JSON | None] = mapped_column(JSON, nullable=True)

    token: Mapped[Token] = relationship(back_populates="market_observations")


class FollowedTrader(Base):
    __tablename__ = "followed_traders"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    username: Mapped[str] = mapped_column(String(128), nullable=False, index=True, unique=True)
    platform_identifier: Mapped[str | None] = mapped_column(String(128), nullable=True)
    profile_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tracking_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    date_added: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sync_error: Mapped[str | None] = mapped_column(String(255), nullable=True)

    wallet_associations: Mapped[list["FollowedWalletAssociation"]] = relationship(back_populates="trader")
    trader_transactions: Mapped[list["TraderTransactionEvent"]] = relationship(back_populates="trader")


class FollowedWalletAssociation(Base):
    __tablename__ = "followed_wallet_associations"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    followed_trader_id: Mapped[int] = mapped_column(ForeignKey("followed_traders.id"), nullable=False, index=True)
    chain_id: Mapped[int | None] = mapped_column(ForeignKey("chains.id"), nullable=True)
    wallet_address: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str] = mapped_column(String(64), default="manual")
    verified_status: Mapped[str] = mapped_column(String(32), default="unverified")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    trader: Mapped[FollowedTrader] = relationship(back_populates="wallet_associations")


class TraderTransactionEvent(Base):
    __tablename__ = "trader_transaction_events"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id"), nullable=False, index=True)
    followed_trader_id: Mapped[int | None] = mapped_column(ForeignKey("followed_traders.id"), nullable=True, index=True)
    wallet_address: Mapped[str] = mapped_column(String(255), nullable=False)
    chain_id: Mapped[int | None] = mapped_column(ForeignKey("chains.id"), nullable=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    tx_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    event_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    event_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    token_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    quote_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    quote_denomination: Mapped[str | None] = mapped_column(String(32), nullable=True)
    estimated_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    market_cap_at_event: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_at_event: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    detection_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    data_quality: Mapped[str] = mapped_column(String(32), default="unknown")
    dedup_key: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)

    token: Mapped[Token] = relationship(back_populates="trader_tx_events")
    trader: Mapped[FollowedTrader | None] = relationship(back_populates="trader_transactions")


class TraderTokenPick(Base):
    __tablename__ = "trader_token_picks"
    __table_args__ = (
        UniqueConstraint("followed_trader_id", "token_id", name="uq_trader_token_pick"),
        Index("ix_trader_token_picks_trader", "followed_trader_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    followed_trader_id: Mapped[int] = mapped_column(ForeignKey("followed_traders.id"), nullable=False)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id"), nullable=False)
    reached_market_cap: Mapped[float] = mapped_column(Float, nullable=False)
    reached_high_market_cap: Mapped[float] = mapped_column(Float, nullable=False)
    current_market_cap: Mapped[float] = mapped_column(Float, nullable=False)
    first_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observation_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class GraduationEvent(Base):
    __tablename__ = "graduation_events"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id"), nullable=False, index=True)
    graduated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    market_cap_at_graduation: Mapped[float | None] = mapped_column(Float, nullable=True)
    threshold_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    highest_observed_market_cap: Mapped[float | None] = mapped_column(Float, nullable=True)
    highest_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    token: Mapped[Token] = relationship(back_populates="graduation_events")


class Favorite(Base):
    __tablename__ = "favorites"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id"), nullable=False, index=True)
    favorited_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    market_cap_at_favorite: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_at_favorite: Mapped[float | None] = mapped_column(Float, nullable=True)
    liquidity_at_favorite: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str | None] = mapped_column(String(128), nullable=True)

    token: Mapped[Token] = relationship(back_populates="favorites")


class DismissedToken(Base):
    __tablename__ = "dismissed_tokens"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id"), nullable=False, index=True)
    dismissed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    restored: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    restored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    token: Mapped[Token] = relationship(back_populates="dismissed_tokens")


class RiskFinding(Base):
    __tablename__ = "risk_findings"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id"), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(32), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    summary: Mapped[str] = mapped_column(String(255), nullable=False)
    evidence: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source: Mapped[str | None] = mapped_column(String(128), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    risk_metadata: Mapped[JSON | None] = mapped_column(JSON, nullable=True)

    token: Mapped[Token] = relationship(back_populates="risk_findings")


class AIIntelligenceRecord(Base):
    __tablename__ = "ai_intelligence_records"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    token_id: Mapped[int | None] = mapped_column(ForeignKey("tokens.id"), nullable=True, index=True)
    record_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(64), default="manual")
    manually_entered: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    data: Mapped[JSON | None] = mapped_column(JSON, nullable=True)

    token: Mapped[Token | None] = relationship(back_populates="ai_records")


class ProviderSyncCheckpoint(Base):
    __tablename__ = "provider_sync_checkpoints"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    provider_name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    state: Mapped[JSON | None] = mapped_column(JSON, nullable=True)


class BackgroundJob(Base):
    __tablename__ = "background_jobs"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    job_type: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued")
    payload: Mapped[JSON | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(255), nullable=True)
    checkpoint: Mapped[JSON | None] = mapped_column(JSON, nullable=True)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    details: Mapped[JSON | None] = mapped_column(JSON, nullable=True)
