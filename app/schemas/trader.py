from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class FollowedTraderCreate(BaseModel):
    username: str = Field(..., min_length=1)
    platform_identifier: str | None = None
    profile_url: str | None = None


class FollowedTraderRead(BaseModel):
    id: int
    username: str
    platform_identifier: str | None = None
    profile_url: str | None = None
    tracking_enabled: bool = True
    date_added: datetime
    last_synced_at: datetime | None = None
