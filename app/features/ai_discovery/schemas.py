from __future__ import annotations

from pydantic import BaseModel


class ManualIntelligenceRecord(BaseModel):
    chain: str
    token_address: str
    notes: str | None = None
    source: str = "manual"
    record_type: str = "research"
