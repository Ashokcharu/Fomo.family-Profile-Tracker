from __future__ import annotations


class ManualDataCollector:
    """Placeholder for structured manual intelligence ingestion."""

    def __init__(self) -> None:
        self.entries: list[dict] = []

    def ingest(self, payload: dict) -> dict:
        self.entries.append(payload)
        return {"status": "accepted", "count": len(self.entries)}
