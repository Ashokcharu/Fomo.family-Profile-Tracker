from __future__ import annotations

import json

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.features.ai_discovery.service import AIDataService
from app.models import AIIntelligenceRecord, BackgroundJob, Chain, DismissedToken, Favorite, MarketObservation, Token


class StubOllama:
    def __init__(self, available: bool, response: dict | None = None) -> None:
        self.available = available
        self.response = response or {"assessments": []}
        self.prompt = ""

    def is_available(self) -> bool:
        return self.available

    def generate(self, prompt: str) -> dict:
        self.prompt = prompt
        return {"status": "ok", "response": json.dumps(self.response)}


def test_discovery_uses_active_tokens_only_and_rejects_unknown_model_ids() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as db:
        chain = Chain(chain_name="Base", chain_slug="base", chain_id="8453")
        db.add(chain)
        db.flush()
        active = Token(chain_id=chain.id, contract_address="active", normalized_address="active", ticker="ACTIVE")
        graduated = Token(chain_id=chain.id, contract_address="graduated", normalized_address="graduated", is_graduated=True)
        favorited = Token(chain_id=chain.id, contract_address="favorite", normalized_address="favorite")
        dismissed = Token(chain_id=chain.id, contract_address="dismissed", normalized_address="dismissed")
        db.add_all([active, graduated, favorited, dismissed])
        db.flush()
        db.add_all([
            Favorite(token_id=favorited.id),
            DismissedToken(token_id=dismissed.id, reason="not tracked"),
            MarketObservation(token_id=active.id, source="test", market_cap=20_000, liquidity=10_000),
        ])
        db.commit()

        ollama = StubOllama(True, {"assessments": [
            {"token_id": active.id, "score": 80, "summary": "Liquidity and preferred market cap."},
            {"token_id": graduated.id, "score": 100, "summary": "Must be ignored."},
        ]})
        service = AIDataService(Settings(ai_discovery_enabled=True, ollama_model="test-model"), ollama)
        result = service.run_discovery(db)

        assert result["status"] == "model_assisted"
        assert result["tokens_scanned"] == 1
        assert [candidate["token_id"] for candidate in result["candidates"]] == [active.id]
        assert result["candidates"][0]["score"] == 62.5
        assert "Must be ignored" not in result["candidates"][0]["summary"]
        prompt_candidates = json.loads(ollama.prompt.split("Candidate: ", 1)[1])
        assert [candidate["token_id"] for candidate in prompt_candidates] == [active.id]
        records = db.query(AIIntelligenceRecord).all()
        assert len(records) == 1
        assert records[0].token_id == active.id
        assert records[0].manually_entered is False


def test_discovery_falls_back_to_tracked_signals_when_ollama_is_unavailable() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as db:
        chain = Chain(chain_name="Base", chain_slug="base", chain_id="8453")
        db.add(chain)
        db.flush()
        token = Token(chain_id=chain.id, contract_address="active", normalized_address="active")
        db.add(token)
        db.flush()
        db.add(MarketObservation(token_id=token.id, source="test", market_cap=20_000, liquidity=10_000))
        db.commit()

        ollama = StubOllama(False)
        result = AIDataService(Settings(ai_discovery_enabled=True), ollama).run_discovery(db)

        assert result["status"] == "heuristic_fallback"
        assert result["tokens_scanned"] == 1
        assert result["candidates"][0]["heuristic_score"] == 55
        assert ollama.prompt == ""


def test_discovery_defers_and_persists_qwen_assessment() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as db:
        chain = Chain(chain_name="Base", chain_slug="base", chain_id="8453")
        db.add(chain)
        db.flush()
        token = Token(chain_id=chain.id, contract_address="active", normalized_address="active")
        db.add(token)
        db.flush()
        db.add(MarketObservation(token_id=token.id, source="test", market_cap=20_000, liquidity=10_000))
        db.commit()

        ollama = StubOllama(True, {"assessments": [
            {"token_id": token.id, "score": 80, "summary": "Liquidity and market cap are strong."},
        ]})
        service = AIDataService(Settings(ai_discovery_enabled=True), ollama)
        initial = service.run_discovery(db, defer_model=True)

        assert initial["status"] == "model_pending"
        assert initial["candidates"][0]["model_pending"] is True
        assert ollama.prompt == ""
        pending = service.get_status(db)
        assert pending["status"] == "model_pending"

        service.complete_model_assessment(
            initial["candidates"][0]["run_id"],
            session_factory=lambda: Session(engine),
        )
        completed = service.get_status(db)

        assert completed["status"] == "model_assisted"
        assert completed["candidates"][0]["model_score"] == 80
        assert completed["candidates"][0]["model_pending"] is False
        db.add(Favorite(token_id=token.id))
        db.commit()
        assert service.get_status(db)["candidates"][0]["is_favorite"] is True


def test_queue_discovery_returns_before_token_scan() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as db:
        service = AIDataService(Settings(ai_discovery_enabled=True), StubOllama(True))

        result = service.queue_discovery(db)

        assert result["status"] == "scan_pending"
        assert result["job_id"] is not None
        assert db.query(BackgroundJob).filter_by(id=result["job_id"], status="queued").count() == 1
        assert db.query(AIIntelligenceRecord).count() == 0


def test_queued_discovery_worker_completes_with_tracked_signal_results() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as db:
        chain = Chain(chain_name="Base", chain_slug="base", chain_id="8453")
        db.add(chain)
        db.flush()
        token = Token(chain_id=chain.id, contract_address="active", normalized_address="active")
        db.add(token)
        db.flush()
        db.add(MarketObservation(token_id=token.id, source="test", market_cap=20_000, liquidity=10_000))
        db.commit()

        service = AIDataService(Settings(ai_discovery_enabled=True), StubOllama(False))
        queued = service.queue_discovery(db)
        service.run_queued_discovery(queued["job_id"], session_factory=lambda: Session(engine))

        completed = service.get_status(db)
        job = db.query(BackgroundJob).filter_by(id=queued["job_id"]).one()
        assert job.status == "completed"
        assert completed["status"] == "heuristic_fallback"
        assert completed["tokens_scanned"] == 1
        assert completed["candidate_count"] == 1