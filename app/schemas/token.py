from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class TokenBase(BaseModel):
    chain: str
    contract_address: str
    ticker: str | None = None
    name: str | None = None


class TokenCreate(TokenBase):
    pass


class TokenRead(TokenBase):
    id: int
    normalized_address: str
    is_graduated: bool = False
    created_at: datetime
    updated_at: datetime


class DiscoveryResult(BaseModel):
    token: str
    source: str
    confidence: float | None = None
    market_cap: float | None = None
    price: float | None = None
    liquidity: float | None = None
