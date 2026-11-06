from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.database import Base, get_db
from app.features.ai_discovery.candidate_features import (
    AI_DISCOVERY_ELIGIBILITY_VERSION,
    build_candidate_features,
)
from app.features.ai_discovery.risk import build_risk_assessments
from app.main import app
from app.features.ai_discovery import service as ai_discovery_service
from app.features.ai_discovery.service import AIDataService
from app.features.tokens.service import token_display_data
from app.integrations.ollama.client import OllamaClient
from app.models import AIDiscoveryModelAssessment, AIDiscoveryOutcome, AIDiscoverySnapshot, AIIntelligenceRecord, BackgroundJob, Chain, DismissedToken, Favorite, FollowedTrader, GraduationEvent, MarketObservation, SecurityAnalysis, Token, TraderTransactionEvent


class StubOllama:
    def __init__(self, available: bool, response: dict | None = None) -> None:
        self.available = available
        self.availability_checks = 0
        self.response = response or {"assessments": []}
        self.prompt = ""
        self.generation_calls = 0

    def is_available(self) -> bool:
        self.availability_checks += 1
        return self.available

    def generate(self, prompt: str) -> dict:
        self.generation_calls += 1
        self.prompt = prompt
        return {"status": "ok", "response": json.dumps(self.response)}


def add_ai_baseline(db: Session, token_id: int, market_cap: float) -> None:
    token = db.get(Token, token_id)
    if token is not None:
        token.first_seen_at = datetime.now(timezone.utc) - timedelta(days=2)
    db.add(MarketObservation(
        token_id=token_id,
        source="test_baseline",
        observed_at=datetime.now(timezone.utc) - timedelta(days=1),
        market_cap=market_cap,
        liquidity=10_000,
    ))
    db.add(AIIntelligenceRecord(
        token_id=token_id,
        record_type="ai_discovery_baseline",
        source="ai_discovery",
        manually_entered=False,
        data={"ai_rmc": market_cap, "ai_rh": market_cap},
    ))


def test_ollama_generation_limits_cpu_and_unloads_model(monkeypatch) -> None:
    captured = {}

    class Response:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {"response": '{"assessments":[]}'}

    def fake_post(url: str, **kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("app.integrations.ollama.client.httpx.post", fake_post)
    result = OllamaClient(num_threads=3).generate("test")

    assert result["status"] == "ok"
    assert captured["json"]["keep_alive"] == 0
    assert captured["json"]["options"]["num_thread"] == 3


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
        add_ai_baseline(db, active.id, 10_000)
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
        assert result["candidates"][0]["score"] == 85
        assert result["candidates"][0]["model_score"] == 80
        assert "Must be ignored" not in result["candidates"][0]["summary"]
        prompt_candidates = json.loads(ollama.prompt.split("Candidate: ", 1)[1])
        assert [candidate["token_id"] for candidate in prompt_candidates] == [active.id]
        records = db.query(AIIntelligenceRecord).filter_by(record_type="ai_discovery_candidate").all()
        assert len(records) == 1
        assert records[0].token_id == active.id
        assert records[0].manually_entered is False
        assessment = db.query(AIDiscoveryModelAssessment).one()
        snapshot = db.query(AIDiscoverySnapshot).one()
        assert assessment.snapshot_id == snapshot.id
        assert assessment.model_name == "test-model"
        assert assessment.score == 80
        assert len(assessment.assessment_hash) == 64


def test_ai_rh_uses_confirmed_observations_after_ai_baseline_only() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as db:
        chain = Chain(chain_name="Base", chain_slug="base", chain_id="8453")
        db.add(chain)
        db.flush()
        baseline_at = datetime.now(timezone.utc)
        tracking_start = baseline_at - timedelta(days=2)
        token = Token(
            chain_id=chain.id,
            contract_address="ai-rh-regression",
            normalized_address="ai-rh-regression",
            first_seen_at=tracking_start,
        )
        db.add(token)
        db.flush()
        db.add_all([
            MarketObservation(
                token_id=token.id,
                source="test",
                observed_at=tracking_start + timedelta(minutes=1),
                market_cap=10_000,
                liquidity=10_000,
            ),
            MarketObservation(
                token_id=token.id,
                source="test",
                observed_at=tracking_start + timedelta(hours=1),
                market_cap=143_631,
                liquidity=10_000,
            ),
            MarketObservation(
                token_id=token.id,
                source="test",
                observed_at=baseline_at - timedelta(minutes=1),
                market_cap=24_127,
                liquidity=10_000,
            ),
            MarketObservation(
                token_id=token.id,
                source="test",
                observed_at=baseline_at + timedelta(minutes=1),
                market_cap=26_000,
                liquidity=10_000,
            ),
            MarketObservation(
                token_id=token.id,
                source="test",
                observed_at=baseline_at + timedelta(minutes=2),
                market_cap=1_000_000,
                liquidity=10_000,
            ),
            MarketObservation(
                token_id=token.id,
                source="test",
                observed_at=baseline_at + timedelta(minutes=3),
                market_cap=26_100,
                liquidity=10_000,
            ),
            MarketObservation(
                token_id=token.id,
                source="test",
                observed_at=baseline_at + timedelta(minutes=4),
                market_cap=26_200,
                liquidity=10_000,
            ),
        ])
        db.add(AIIntelligenceRecord(
            token_id=token.id,
            record_type="ai_discovery_baseline",
            source="ai_discovery",
            manually_entered=False,
            created_at=baseline_at,
            data={"ai_rmc": 24_127, "ai_rh": 143_631},
        ))
        db.add(AIIntelligenceRecord(
            token_id=token.id,
            record_type="ai_discovery_candidate",
            source="ai_discovery",
            manually_entered=False,
            created_at=baseline_at,
            data={
                "token_id": token.id,
                "run_id": "regression-run",
                "rank": 1,
                "tokens_scanned": 1,
                "last_run_at": baseline_at.isoformat(),
                "ai_discovery_since": baseline_at.isoformat(),
                "ai_rmc": 24_127,
                "ai_rh": 143_631,
                "heuristic_score": 70,
                "score": 70,
                "continuation_adjustment": 0,
                "review_mode": "signal_only",
                "inactive": False,
            },
        ))
        db.commit()

        service = AIDataService(Settings(ai_discovery_enabled=True), StubOllama(False))
        candidate = service.get_status(db)["candidates"][0]

        assert candidate["market_cap"] == 26_200
        assert candidate["ai_rh"] == 26_200
        assert db.query(AIIntelligenceRecord).filter_by(
            token_id=token.id,
            record_type="ai_discovery_baseline",
        ).one().data["ai_rh"] == 26_200
        assert db.query(AIIntelligenceRecord).filter_by(
            token_id=token.id,
            record_type="ai_discovery_candidate",
        ).one().data["ai_rh"] == 26_200


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
        add_ai_baseline(db, token.id, 10_000)
        db.commit()

        ollama = StubOllama(False)
        result = AIDataService(Settings(ai_discovery_enabled=True), ollama).run_discovery(db)

        assert result["status"] == "heuristic_fallback"
        assert result["tokens_scanned"] == 1
        assert result["candidates"][0]["heuristic_score"] == 85
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
        add_ai_baseline(db, token.id, 10_000)
        db.commit()

        ollama = StubOllama(True, {"assessments": [
            {"token_id": token.id, "score": 80, "summary": "Liquidity and market cap are strong."},
        ]})
        service = AIDataService(Settings(ai_discovery_enabled=True), ollama)
        initial = service.run_discovery(db, defer_model=True)

        assert initial["status"] == "model_pending"
        assert initial["candidates"][0]["model_pending"] is True
        initial_signal_score = initial["candidates"][0]["score"]
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
        assert db.query(AIDiscoveryModelAssessment).count() == 1
        assert completed["candidates"][0]["score"] == initial_signal_score
        assert completed["candidates"][0]["model_pending"] is False
        assert datetime.fromisoformat(completed["candidates"][0]["ai_discovery_since"]).tzinfo == timezone.utc
        db.add(Favorite(token_id=token.id))
        db.commit()
        assert service.get_status(db)["candidates"] == []


def test_ai_bulk_delete_routes_accept_token_ids_before_detail_routes() -> None:
    client = TestClient(app)
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autocommit=False, autoflush=False, future=True)

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        with Session(engine) as db:
            chain = Chain(chain_name="Base", chain_slug="base", chain_id="8453")
            db.add(chain)
            db.flush()
            token = Token(chain_id=chain.id, contract_address="bulk-delete-token", normalized_address="bulk-delete-token")
            db.add(token)
            db.flush()
            token_id = token.id
            db.add(AIIntelligenceRecord(
                token_id=token_id,
                record_type="ai_discovery_candidate",
                source="ai_discovery",
                data={"token_id": token_id, "score": 75, "name": "Bulk Delete"},
            ))
            db.add(AIIntelligenceRecord(
                token_id=token_id,
                record_type="ai_discovery_top_pick",
                source="ai_discovery",
                data={"token_id": token_id, "score": 75, "active": True, "name": "Bulk Delete"},
            ))
            db.commit()

        candidate_response = client.request(
            "DELETE",
            "/api/v1/ai-discovery/candidates/bulk-delete",
            json={"token_ids": [token_id]},
        )
        top_pick_response = client.request(
            "DELETE",
            "/api/v1/ai-discovery/top-picks/bulk-delete",
            json={"token_ids": [token_id]},
        )

        assert candidate_response.status_code == 200
        assert candidate_response.json()["deleted"] == 1
        assert top_pick_response.status_code == 200
        assert top_pick_response.json()["deleted"] == 1
    finally:
        app.dependency_overrides.clear()


def test_ai_history_flag_is_returned_for_tracked_and_favorite_tokens() -> None:
    client = TestClient(app)
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autocommit=False, autoflush=False, future=True)

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        with Session(engine) as db:
            chain = Chain(chain_name="Base", chain_slug="base", chain_id="8453")
            db.add(chain)
            db.flush()
            token = Token(
                chain_id=chain.id,
                contract_address="ai-history-token",
                normalized_address="ai-history-token",
            )
            db.add(token)
            db.flush()
            token_id = token.id
            db.add_all([
                AIIntelligenceRecord(
                    token_id=token_id,
                    record_type="ai_discovery_candidate",
                    source="ai_discovery",
                    data={
                        "token_id": token.id,
                        "eligibility_version": AI_DISCOVERY_ELIGIBILITY_VERSION,
                    },
                ),
                AIIntelligenceRecord(
                    token_id=token_id,
                    record_type="ai_discovery_exclusion",
                    source="ai_discovery",
                    manually_entered=True,
                    data={"reason": "manually_removed"},
                ),
            ])
            db.commit()

        tracked = client.get("/api/v1/tokens/").json()
        with Session(engine) as db:
            db.add(Favorite(token_id=token_id))
            db.add(GraduationEvent(
                token_id=token_id,
                market_cap_at_graduation=100_000,
                threshold_usd=100_000,
                highest_observed_market_cap=120_000,
            ))
            db.commit()
        favorites = client.get("/api/v1/favorites/").json()
        graduated = client.get("/api/v1/graduated-tokens/").json()

        assert tracked[0]["has_ai_discovery_history"] is True
        assert favorites[0]["has_ai_discovery_history"] is True
        assert graduated[0]["has_ai_discovery_history"] is True
    finally:
        app.dependency_overrides.clear()


def test_legacy_ai_candidate_history_does_not_show_new_badge() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as db:
        chain = Chain(chain_name="Base", chain_slug="base", chain_id="8453")
        db.add(chain)
        db.flush()
        token = Token(
            chain_id=chain.id,
            contract_address="legacy-ai-badge",
            normalized_address="legacy-ai-badge",
        )
        db.add(token)
        db.flush()
        db.add(AIIntelligenceRecord(
            token_id=token.id,
            record_type="ai_discovery_candidate",
            source="ai_discovery",
            data={"token_id": token.id, "score": 80},
        ))
        db.commit()

        assert token_display_data(db, [token.id])[token.id]["has_ai_discovery_history"] is False


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
        add_ai_baseline(db, token.id, 10_000)
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


def test_removed_candidate_stays_tracked_but_is_excluded_from_future_ai_runs() -> None:
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
        add_ai_baseline(db, token.id, 10_000)
        db.commit()

        service = AIDataService(Settings(ai_discovery_enabled=True), StubOllama(False))
        service.run_discovery(db, use_model=False)
        assert service.get_status(db)["candidate_count"] == 1
        assert token_display_data(db, [token.id])[token.id]["has_ai_discovery_history"] is True

        assert service.remove_candidate(db, token.id) is True
        assert service.get_status(db)["candidate_count"] == 0
        assert token_display_data(db, [token.id])[token.id]["has_ai_discovery_history"] is True
        analyzed = service.list_analyzed_candidates(db)
        assert len(analyzed) == 1
        assert analyzed[0]["token_id"] == token.id
        assert analyzed[0]["score"] is not None
        assert analyzed[0]["ai_analyzed_at"] is not None
        assert analyzed[0]["archive_reason"] == "manually_removed"
        db.add(MarketObservation(
            token_id=token.id,
            source="test",
            observed_at=datetime.now(timezone.utc) + timedelta(seconds=1),
            market_cap=30_000,
            liquidity=12_000,
        ))
        db.commit()
        refreshed_analyzed = service.list_analyzed_candidates(db)[0]
        assert refreshed_analyzed["market_cap"] == 30_000
        assert refreshed_analyzed["liquidity"] == 12_000
        assert refreshed_analyzed["ai_rh"] == 30_000
        service.run_discovery(db, use_model=False)

        assert service.get_status(db)["candidate_count"] == 0
        assert service.list_analyzed_candidates(db)[0]["token_id"] == token.id
        assert db.get(Token, token.id) is not None


def test_discovery_limits_results_to_top_50_growth_eligible_candidates(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as db:
        chain = Chain(chain_name="Base", chain_slug="base", chain_id="8453")
        db.add(chain)
        db.flush()
        tokens = [
            Token(chain_id=chain.id, contract_address=f"token-{index}", normalized_address=f"token-{index}")
            for index in range(50)
        ]
        db.add_all(tokens)
        db.flush()
        db.add_all([
            MarketObservation(token_id=token.id, source="test", market_cap=20_000, liquidity=10_000)
            for token in tokens
        ])
        for token in tokens:
            add_ai_baseline(db, token.id, 10_000)
        db.commit()

        original_builder = ai_discovery_service.build_candidate_features
        replacement_id = None

        def score_new_token_higher(token_data, settings):
            features = original_builder(token_data, settings)
            features["heuristic_score"] = 90 if token_data["id"] == replacement_id else 60
            return features

        monkeypatch.setattr(ai_discovery_service, "build_candidate_features", score_new_token_higher)
        service = AIDataService(Settings(ai_discovery_enabled=True), StubOllama(False))
        first_run = service.run_discovery(db, use_model=False)
        assert len(first_run["candidates"]) == 50

        replacement = Token(chain_id=chain.id, contract_address="replacement", normalized_address="replacement")
        db.add(replacement)
        db.flush()
        replacement_id = replacement.id
        db.add(MarketObservation(token_id=replacement.id, source="test", market_cap=20_000, liquidity=10_000))
        add_ai_baseline(db, replacement.id, 10_000)
        db.commit()

        second_run = service.run_discovery(db, use_model=False)
        candidate_ids = {candidate["token_id"] for candidate in second_run["candidates"]}
        assert len(second_run["candidates"]) == 50
        assert replacement.id in candidate_ids
        assert sum(token.id in candidate_ids for token in tokens) == 49


def test_low_signal_candidate_remains_active_while_growth_eligible(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as db:
        chain = Chain(chain_name="Base", chain_slug="base", chain_id="8453")
        db.add(chain)
        db.flush()
        token = Token(chain_id=chain.id, contract_address="low-signal", normalized_address="low-signal")
        db.add(token)
        db.flush()
        db.add(MarketObservation(token_id=token.id, source="test", market_cap=20_000, liquidity=10_000))
        add_ai_baseline(db, token.id, 10_000)
        db.commit()

        original_builder = ai_discovery_service.build_candidate_features

        def score_candidate_low(token_data, settings):
            features = original_builder(token_data, settings)
            features["heuristic_score"] = 20
            return features

        monkeypatch.setattr(ai_discovery_service, "build_candidate_features", score_candidate_low)
        service = AIDataService(Settings(ai_discovery_enabled=True), StubOllama(False))
        first_result = service.run_discovery(db, use_model=False)
        assert len(first_result["candidates"]) == 1

        result = service.run_discovery(db, use_model=False)

        assert len(result["candidates"]) == 1
        assert result["candidates"][0]["score"] < 50
        assert service.list_analyzed_candidates(db) == []


def test_early_positive_movement_gets_a_bounded_momentum_signal() -> None:
    settings = Settings()
    minimum_growth = build_candidate_features({
        "id": 1,
        "market_cap": 5_500,
        "liquidity": 5_000,
        "growth_multiple": 1.10,
    }, settings)
    stronger_growth = build_candidate_features({
        "id": 2,
        "market_cap": 6_000,
        "liquidity": 5_000,
        "growth_multiple": 1.20,
    }, settings)

    assert minimum_growth["score_breakdown"]["observed_growth"] == 10.0
    assert minimum_growth["score_breakdown"]["early_momentum"] == 0.0
    assert stronger_growth["score_breakdown"]["observed_growth"] == 20.0
    assert stronger_growth["score_breakdown"]["early_momentum"] == 25.0


def test_ai_discovery_requires_minimum_liquidity_and_hides_stale_candidates() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as db:
        chain = Chain(chain_name="Base", chain_slug="base", chain_id="8453")
        db.add(chain)
        db.flush()
        below_threshold = Token(
            chain_id=chain.id,
            contract_address="below-liquidity",
            normalized_address="below-liquidity",
        )
        at_threshold = Token(
            chain_id=chain.id,
            contract_address="at-liquidity",
            normalized_address="at-liquidity",
        )
        db.add_all([below_threshold, at_threshold])
        db.flush()
        add_ai_baseline(db, below_threshold.id, 10_000)
        add_ai_baseline(db, at_threshold.id, 10_000)
        observed_at = datetime.now(timezone.utc)
        db.add_all([
            MarketObservation(
                token_id=below_threshold.id,
                source="test",
                observed_at=observed_at,
                market_cap=12_000,
                liquidity=2_499,
            ),
            MarketObservation(
                token_id=at_threshold.id,
                source="test",
                observed_at=observed_at,
                market_cap=12_000,
                liquidity=2_500,
            ),
        ])
        db.commit()

        service = AIDataService(
            Settings(ai_discovery_enabled=True, ai_discovery_min_liquidity_usd=2_500),
            StubOllama(False),
        )
        result = service.run_discovery(db, use_model=False)

        assert [candidate["token_id"] for candidate in result["candidates"]] == [at_threshold.id]

        db.add(MarketObservation(
            token_id=at_threshold.id,
            source="test",
            observed_at=observed_at + timedelta(seconds=5),
            market_cap=12_000,
            liquidity=2_499,
        ))
        db.add(AIIntelligenceRecord(
            token_id=at_threshold.id,
            record_type="ai_discovery_top_pick",
            source="ai_discovery",
            manually_entered=False,
            data={"active": True, "token_id": at_threshold.id},
        ))
        db.commit()

        assert service.get_status(db)["candidates"] == []
        assert service.get_status(db)["top_picks"] == []


def test_growth_gate_allows_automatic_reentry_and_reuses_unchanged_ollama_assessment() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as db:
        chain = Chain(chain_name="Base", chain_slug="base", chain_id="8453")
        db.add(chain)
        db.flush()
        token = Token(chain_id=chain.id, contract_address="growth-gate", normalized_address="growth-gate")
        db.add(token)
        db.flush()
        db.add(MarketObservation(
            token_id=token.id,
            source="test",
            market_cap=10_000,
            liquidity=10_000,
        ))
        db.commit()

        observed_at = datetime.now(timezone.utc) + timedelta(seconds=1)

        def observe(market_cap: float, seconds: int) -> None:
            db.add(MarketObservation(
                token_id=token.id,
                source="test",
                observed_at=observed_at + timedelta(seconds=seconds),
                market_cap=market_cap,
                liquidity=10_000,
            ))
            db.commit()

        ollama = StubOllama(True, {"assessments": [
            {"token_id": token.id, "score": 80, "summary": "Growth-qualified evidence."},
        ]})
        service = AIDataService(Settings(ai_discovery_enabled=True), ollama)

        first = service.run_discovery(db)
        assert first["candidates"] == []
        assert db.query(AIIntelligenceRecord).filter_by(
            token_id=token.id,
            record_type="ai_discovery_baseline",
        ).count() == 0
        assert ollama.availability_checks == 0

        observe(10_999, 1)
        assert service.run_discovery(db)["candidates"] == []
        assert ollama.generation_calls == 0

        observe(11_000, 2)
        qualified = service.run_discovery(db)
        assert round(qualified["candidates"][0]["growth_percent"], 2) == 10.0
        assert qualified["candidates"][0]["ai_rmc"] == 11_000
        assert qualified["candidates"][0]["ai_discovery_since"] == qualified["last_run_at"]
        assert ollama.generation_calls == 1

        continuing = service.run_discovery(db)
        assert continuing["candidates"][0]["ai_rmc"] == 11_000
        assert continuing["candidates"][0]["ai_discovery_since"] == qualified["candidates"][0]["ai_discovery_since"]
        assert ollama.generation_calls == 1

        observe(10_900, 3)
        dropped = service.run_discovery(db)
        assert dropped["candidates"] == []
        assert service.get_status(db)["candidate_count"] == 0
        assert service.list_analyzed_candidates(db)[0]["archive_reason"] == "growth_below_threshold"

        observe(12_000, 4)
        requalified = service.run_discovery(db)
        assert [candidate["token_id"] for candidate in requalified["candidates"]] == [token.id]
        assert requalified["candidates"][0]["ai_discovery_since"] == requalified["last_run_at"]
        assert requalified["candidates"][0]["ai_discovery_since"] != qualified["candidates"][0]["ai_discovery_since"]
        assert requalified["candidates"][0]["ai_rmc"] == 12_000
        assert requalified["candidates"][0]["ai_rh"] == 12_000
        assert requalified["candidates"][0]["ai_growth_multiple"] == 1
        baseline = db.query(AIIntelligenceRecord).filter_by(
            token_id=token.id,
            record_type="ai_discovery_baseline",
        ).one()
        assert baseline.data == {"ai_rmc": 12_000, "ai_rh": 12_000}
        assert baseline.created_at.replace(tzinfo=timezone.utc).isoformat() == (
            requalified["candidates"][0]["ai_discovery_since"]
        )
        snapshots = db.query(AIDiscoverySnapshot).filter_by(token_id=token.id).order_by(
            AIDiscoverySnapshot.captured_at
        ).all()
        assert snapshots[0].market_cap == 11_000
        assert snapshots[-1].market_cap == 12_000
        assert ollama.generation_calls == 2

        assert service.remove_candidate(db, token.id)
        observe(15_000, 5)
        manually_excluded = service.run_discovery(db)
        assert manually_excluded["candidates"] == []
        assert ollama.generation_calls == 2


def test_stale_ai_baseline_does_not_override_tracked_growth_eligibility() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as db:
        chain = Chain(chain_name="BNB Chain", chain_slug="bnb", chain_id="56")
        db.add(chain)
        db.flush()
        token = Token(
            chain_id=chain.id,
            contract_address="stale-ai-baseline",
            normalized_address="stale-ai-baseline",
            first_seen_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        db.add(token)
        db.flush()
        now = datetime.now(timezone.utc)
        db.add_all([
            MarketObservation(
                token_id=token.id,
                source="test",
                observed_at=now - timedelta(minutes=5),
                market_cap=8_716.72,
                liquidity=10_000,
            ),
            MarketObservation(
                token_id=token.id,
                source="test",
                observed_at=now,
                market_cap=8_723.46,
                liquidity=10_000,
            ),
            AIIntelligenceRecord(
                token_id=token.id,
                record_type="ai_discovery_baseline",
                source="ai_discovery",
                manually_entered=False,
                data={"ai_rmc": 8_723.46, "ai_rh": 8_723.46},
            ),
            AIIntelligenceRecord(
                token_id=token.id,
                record_type="ai_discovery_candidate",
                source="ai_discovery",
                manually_entered=False,
                data={"run_id": "old-run", "rank": 1, "token_id": token.id, "score": 58},
            ),
        ])
        db.commit()

        service = AIDataService(Settings(ai_discovery_enabled=True), StubOllama(False))
        result = service.run_discovery(db, use_model=False)

        assert result["candidates"] == []
        excluded = db.query(AIIntelligenceRecord).filter_by(
            token_id=token.id,
            record_type="ai_discovery_exclusion",
        ).one()
        assert excluded.data["reason"] == "growth_below_threshold"


def test_status_hides_saved_candidate_that_no_longer_meets_growth_gate() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as db:
        chain = Chain(chain_name="BNB Chain", chain_slug="bnb", chain_id="56")
        db.add(chain)
        db.flush()
        token = Token(
            chain_id=chain.id,
            contract_address="status-growth-gate",
            normalized_address="status-growth-gate",
            first_seen_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        db.add(token)
        db.flush()
        now = datetime.now(timezone.utc)
        db.add_all([
            MarketObservation(
                token_id=token.id,
                source="test",
                observed_at=now - timedelta(minutes=5),
                market_cap=10_000,
                liquidity=10_000,
            ),
            MarketObservation(
                token_id=token.id,
                source="test",
                observed_at=now,
                market_cap=10_900,
                liquidity=10_000,
            ),
            AIIntelligenceRecord(
                token_id=token.id,
                record_type="ai_discovery_candidate",
                source="ai_discovery",
                manually_entered=False,
                data={
                    "token_id": token.id,
                    "run_id": "saved-run",
                    "rank": 1,
                    "score": 80,
                    "heuristic_score": 80,
                    "growth_percent": 20,
                    "growth_multiple": 1.2,
                    "market_cap": 12_000,
                    "last_run_at": now.isoformat(),
                },
            ),
        ])
        db.commit()

        service = AIDataService(Settings(ai_discovery_enabled=True), StubOllama(False))
        stale_status = service.get_status(db)
        assert stale_status["candidate_count"] == 0
        assert stale_status["candidates"] == []

        db.add(MarketObservation(
            token_id=token.id,
            source="test",
            observed_at=now + timedelta(seconds=1),
            market_cap=12_000,
            liquidity=10_000,
        ))
        db.commit()

        recovered_status = service.get_status(db)
        assert recovered_status["candidate_count"] == 1
        assert round(recovered_status["candidates"][0]["growth_percent"], 2) == 20
        assert recovered_status["candidates"][0]["market_cap"] == 12_000


def test_airmc_feedback_and_top_pick_lifecycle_are_persisted() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as db:
        chain = Chain(chain_name="Base", chain_slug="base", chain_id="8453")
        db.add(chain)
        db.flush()
        token = Token(chain_id=chain.id, contract_address="early", normalized_address="early")
        traders = [FollowedTrader(username=f"trader-{index}") for index in range(2)]
        db.add_all([token, *traders])
        db.flush()
        db.add_all([
            MarketObservation(token_id=token.id, source="test", market_cap=5_000, liquidity=10_000),
            MarketObservation(token_id=token.id, source="test", market_cap=5_100, liquidity=10_000),
            *[
                TraderTransactionEvent(
                    token_id=token.id,
                    followed_trader_id=trader.id,
                    wallet_address=f"wallet-{index}",
                    event_type="buy",
                    source="test",
                )
                for index, trader in enumerate(traders)
            ],
        ])
        db.commit()

        service = AIDataService(Settings(ai_discovery_enabled=True), StubOllama(False))
        first = service.run_discovery(db, use_model=False)
        assert first["candidates"] == []
        assert db.query(AIIntelligenceRecord).filter_by(
            token_id=token.id,
            record_type="ai_discovery_baseline",
        ).count() == 0

        service.record_feedback(db, token.id, "positive")
        db.add(MarketObservation(
            token_id=token.id,
            source="test",
            observed_at=datetime.now(timezone.utc) + timedelta(seconds=1),
            market_cap=6_000,
            liquidity=10_000,
        ))
        db.commit()
        second = service.run_discovery(db, use_model=False)
        assert second["candidates"][0]["ai_rh"] == 6_000
        original_ai_rmc = second["candidates"][0]["ai_rmc"]
        assert original_ai_rmc == 6_000
        assert second["candidates"][0]["feedback"] == "positive"
        assert second["candidates"][0]["score"] == (
            second["candidates"][0]["heuristic_score"]
            + second["candidates"][0]["feedback_adjustment"]
            + second["candidates"][0]["continuation_adjustment"]
        )
        assert second["candidates"][0]["continuation_adjustment"] == 0
        assert second["top_picks"] == []

        db.add(MarketObservation(
            token_id=token.id,
            source="test",
            observed_at=datetime.now(timezone.utc) + timedelta(seconds=2),
            market_cap=8_000,
            liquidity=10_000,
        ))
        db.commit()
        third = service.run_discovery(db, use_model=False)
        assert third["candidates"][0]["continuation_adjustment"] > 0
        assert len(third["top_picks"]) == 1
        assert third["top_picks"][0]["ai_rmc"] == original_ai_rmc
        assert third["top_picks"][0]["ai_rh"] == 8_000
        assert third["top_picks"][0]["t_rmc"] == 8_000
        assert third["top_picks"][0]["t_rh"] == 8_000
        assert third["top_picks"][0]["ai_growth_multiple"] > 1

        db.add(MarketObservation(
            token_id=token.id,
            source="test",
            observed_at=datetime.now(timezone.utc) + timedelta(seconds=3),
            market_cap=9_000,
            liquidity=10_000,
        ))
        db.commit()
        refreshed_market = service.get_status(db)["top_picks"][0]
        assert refreshed_market["market_cap"] == 9_000
        assert refreshed_market["t_rmc"] == 8_000
        assert refreshed_market["t_rh"] == 9_000

        latest_candidate = db.query(AIIntelligenceRecord).filter_by(
            record_type="ai_discovery_candidate",
            source="ai_discovery",
        ).order_by(AIIntelligenceRecord.id.desc()).first()
        latest_candidate.data = {key: value for key, value in latest_candidate.data.items() if key != "ai_rh"}
        active_pick = db.query(AIIntelligenceRecord).filter_by(
            record_type="ai_discovery_top_pick",
            source="ai_discovery",
        ).order_by(AIIntelligenceRecord.id.desc()).first()
        active_pick.data = {key: value for key, value in active_pick.data.items() if key != "ai_rh"}
        db.commit()
        refreshed = service.get_status(db)
        assert refreshed["candidates"][0]["ai_rh"] == 9_000
        assert refreshed["top_picks"][0]["ai_rh"] == 9_000

        assert service.remove_top_pick(db, token.id)
        assert service.get_status(db)["top_picks"] == []


def test_top_pick_requires_positive_growth_from_both_baselines() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as db:
        chain = Chain(chain_name="Base", chain_slug="base", chain_id="8453")
        db.add(chain)
        db.flush()
        below = Token(chain_id=chain.id, contract_address="recovering", normalized_address="recovering")
        boundary = Token(chain_id=chain.id, contract_address="at-boundary", normalized_address="at-boundary")
        above = Token(chain_id=chain.id, contract_address="above-boundary", normalized_address="above-boundary")
        db.add_all([below, boundary, above])
        db.flush()
        service = AIDataService(Settings(ai_discovery_enabled=True), StubOllama(False))
        candidates = [
            {"token_id": below.id, "score": 70, "ai_rmc": 5_000, "ai_growth_multiple": 1.02, "growth_multiple": 0.99, "market_cap": 5_100},
            {"token_id": boundary.id, "score": 70, "ai_rmc": 5_000, "ai_growth_multiple": 2.0, "growth_multiple": 2.0, "market_cap": 10_000},
            {"token_id": above.id, "score": 70, "ai_rmc": 5_000, "ai_growth_multiple": 2.001, "growth_multiple": 2.001, "market_cap": 10_005},
        ]
        market_features = {
            candidate["token_id"]: {
                "market_cap": candidate["market_cap"],
                "growth_multiple": candidate["growth_multiple"],
            }
            for candidate in candidates
        }

        service._update_top_picks(db, candidates, market_features)
        picks = db.query(AIIntelligenceRecord).filter_by(record_type="ai_discovery_top_pick").all()
        assert len(picks) == 1
        assert picks[0].token_id == boundary.id


def test_active_top_picks_include_current_token_image_for_existing_records() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as db:
        chain = Chain(chain_name="Base", chain_slug="base", chain_id="8453")
        db.add(chain)
        db.flush()
        token = Token(
            chain_id=chain.id,
            contract_address="image-token",
            normalized_address="image-token",
            ticker="IMAGE",
            name="Image Token",
        )
        db.add(token)
        db.flush()
        db.add_all([
            AIIntelligenceRecord(
                token_id=token.id,
                record_type="ai_discovery_top_pick",
                source="ai_discovery",
                manually_entered=False,
                data={"active": True, "name": "Image Token"},
            ),
            MarketObservation(
                token_id=token.id,
                source="test",
                observed_at=datetime.now(timezone.utc),
                market_cap=6_000,
                liquidity=2_500,
                raw_data={"image_url": "https://cdn.example/image-token.png"},
            ),
        ])
        db.commit()

        service = AIDataService(Settings(ai_discovery_enabled=True), StubOllama(False))
        picks = service._active_top_picks(db)

        assert len(picks) == 1
        assert picks[0]["image_url"] == "https://cdn.example/image-token.png"
        assert picks[0]["ticker"] == "IMAGE"
        assert picks[0]["contract_address"] == "image-token"


def test_removing_top_pick_refills_from_latest_eligible_candidate() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as db:
        chain = Chain(chain_name="Base", chain_slug="base", chain_id="8453")
        db.add(chain)
        db.flush()
        tokens = [
            Token(chain_id=chain.id, contract_address=f"token-{index}", normalized_address=f"token-{index}")
            for index in range(3)
        ]
        db.add_all(tokens)
        db.flush()
        candidates = [
            {
                "token_id": token.id,
                "run_id": "latest-run",
                "rank": index + 1,
                "last_run_at": datetime.now(timezone.utc).isoformat(),
                "tokens_scanned": 3,
                "heuristic_score": 80,
                "score": 80,
                "ai_rmc": 5_000,
                "ai_growth_multiple": 1.2,
                "growth_multiple": 1.2,
                "market_cap": 6_000,
                "liquidity": 5_000,
            }
            for index, token in enumerate(tokens)
        ]
        db.add_all([
            AIIntelligenceRecord(
                token_id=candidate["token_id"],
                record_type="ai_discovery_candidate",
                source="ai_discovery",
                manually_entered=False,
                data=candidate,
            )
            for candidate in candidates
        ])
        db.add(AIIntelligenceRecord(
            token_id=tokens[0].id,
            record_type="ai_discovery_top_pick",
            source="ai_discovery",
            manually_entered=False,
            data={**candidates[0], "active": True},
        ))
        db.add_all([
            MarketObservation(
                token_id=token.id,
                source="test",
                market_cap=6_000,
                liquidity=5_000,
            )
            for token in tokens
        ])
        db.commit()

        service = AIDataService(Settings(ai_discovery_enabled=True), StubOllama(False))
        assert service.remove_top_pick(db, tokens[0].id)

        active_picks = service._active_top_picks(db)
        assert [pick["token_id"] for pick in active_picks] == [tokens[1].id, tokens[2].id]

        tokens[1].is_graduated = True
        db.commit()
        status = service.get_status(db)
        assert [pick["token_id"] for pick in status["top_picks"]] == [tokens[2].id]

        with Session(engine) as verification_db:
            persisted_picks = service._active_top_picks(verification_db)
            assert [pick["token_id"] for pick in persisted_picks] == [tokens[2].id]


def test_automatic_feedback_tracks_airmc_drawdown_and_three_x_high() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as db:
        chain = Chain(chain_name="Base", chain_slug="base", chain_id="8453")
        db.add(chain)
        db.flush()
        token = Token(chain_id=chain.id, contract_address="outcome", normalized_address="outcome")
        token.first_seen_at = datetime.now(timezone.utc) - timedelta(days=1)
        db.add(token)
        db.flush()
        observed_at = datetime.now(timezone.utc)
        baseline = AIIntelligenceRecord(
            token_id=token.id,
            record_type="ai_discovery_baseline",
            source="ai_discovery",
            manually_entered=False,
            data={"ai_rmc": 5_000},
        )
        prior_candidate = AIIntelligenceRecord(
            token_id=token.id,
            record_type="ai_discovery_candidate",
            source="ai_discovery",
            manually_entered=False,
            data={"run_id": "prior-run", "rank": 1, "token_id": token.id, "score": 55},
        )
        db.add_all([baseline, prior_candidate])
        db.flush()
        db.add_all([
            MarketObservation(
                token_id=token.id,
                source="test",
                observed_at=observed_at - timedelta(seconds=1),
                market_cap=20_000,
            ),
            MarketObservation(
                token_id=token.id,
                source="test",
                observed_at=observed_at + timedelta(seconds=1),
                market_cap=4_900,
            ),
        ])
        db.commit()

        service = AIDataService(Settings(ai_discovery_enabled=True), StubOllama(False))
        service.run_discovery(db, use_model=False)
        feedback = db.query(AIIntelligenceRecord).filter_by(
            token_id=token.id,
            record_type="ai_discovery_feedback",
        ).order_by(AIIntelligenceRecord.id.desc()).first()
        assert feedback.data["rating"] == "negative"
        assert feedback.data["reason"] == "market_cap_fell_below_ai_rmc"

        db.add(MarketObservation(
            token_id=token.id,
            source="test",
            observed_at=observed_at + timedelta(seconds=2),
            market_cap=15_000,
        ))
        db.add(MarketObservation(
            token_id=token.id,
            source="test",
            observed_at=observed_at + timedelta(seconds=3),
            market_cap=15_100,
        ))
        db.commit()
        service.run_discovery(db, use_model=False)
        feedback = db.query(AIIntelligenceRecord).filter_by(
            token_id=token.id,
            record_type="ai_discovery_feedback",
        ).order_by(AIIntelligenceRecord.id.desc()).first()
        assert feedback.data["rating"] == "positive"
        assert feedback.data["reason"] == "observed_high_reached_3x_ai_rmc"


def test_ollama_availability_is_cached_between_status_checks() -> None:
    ollama = StubOllama(True)
    service = AIDataService(Settings(ai_discovery_enabled=True), ollama)

    assert service._model_available() is True
    assert service._model_available() is True
    assert ollama.availability_checks == 1


def test_deterministic_risk_reports_liquidity_divergence_and_data_coverage() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        chain = Chain(chain_name="Base", chain_slug="base", chain_id="8453")
        db.add(chain)
        db.flush()
        token = Token(chain_id=chain.id, contract_address="risk", normalized_address="risk")
        db.add(token)
        db.flush()
        db.add_all([
            MarketObservation(
                token_id=token.id,
                source="dexscreener",
                observed_at=now - timedelta(hours=2),
                market_cap=10_000,
                price=0.01,
                liquidity=10_000,
                volume=2_000,
            ),
            MarketObservation(
                token_id=token.id,
                source="dexscreener",
                observed_at=now,
                market_cap=15_000,
                price=0.015,
                liquidity=5_000,
                volume=4_000,
            ),
        ])
        db.commit()

        risk = build_risk_assessments(db, [token.id], Settings(), now)[token.id]

        assert risk["risk_score"] == 50
        assert risk["risk_level"] == "moderate"
        assert risk["data_completeness_percent"] == 71.4
        assert risk["data_completeness_missing"] == [
            "holder_count",
            "top_token_account_concentration",
        ]
        factors = {factor["name"]: factor for factor in risk["factors"]}
        assert factors["liquidity_trend"]["status"] == "risk"
        assert factors["market_cap_liquidity_divergence"]["status"] == "risk"
        assert factors["holder_concentration"]["status"] == "unknown"
        assert factors["market_data_freshness"]["source"] == "dexscreener"


def test_risk_uses_only_fresh_verified_helius_account_concentration() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        chain = Chain(chain_name="Solana", chain_slug="solana")
        db.add(chain)
        db.flush()
        token = Token(chain_id=chain.id, contract_address="concentration", normalized_address="concentration")
        db.add(token)
        db.flush()
        db.add(MarketObservation(
            token_id=token.id,
            source="dexscreener",
            observed_at=now,
            market_cap=12_000,
            liquidity=8_000,
            price=0.01,
            volume=500,
            raw_data={"holder_count": 250},
        ))
        db.add(SecurityAnalysis(
            token_id=token.id,
            chain_name="Solana",
            token_address=token.normalized_address,
            risk_score=20,
            risk_level="low",
            analysis_status="ready",
            last_analyzed_at=now,
            analysis_payload={
                "holder_concentration": {
                    "status": "verified",
                    "concentration_provider": "helius",
                    "concentration_basis": "top_token_accounts_as_share_of_total_supply",
                    "top_10_percent": 25,
                    "concentration_observed_at": now.isoformat(),
                },
            },
        ))
        db.commit()

        risk = build_risk_assessments(db, [token.id], Settings(), now)[token.id]
        factor = next(
            item for item in risk["factors"]
            if item["name"] == "top_token_account_concentration"
        )
        assert factor["status"] == "risk"
        assert factor["score"] == 100
        assert factor["source"] == "helius"
        assert "not necessarily unique wallets" in factor["evidence"]
        assert risk["data_completeness_percent"] == 85.7

        analysis = db.query(SecurityAnalysis).filter_by(token_id=token.id).one()
        analysis.analysis_payload = {
            **analysis.analysis_payload,
            "holder_concentration": {
                **analysis.analysis_payload["holder_concentration"],
                "concentration_observed_at": (now - timedelta(hours=2)).isoformat(),
            },
        }
        db.commit()
        stale = build_risk_assessments(db, [token.id], Settings(), now)[token.id]
        stale_factor = next(
            item for item in stale["factors"]
            if item["name"] == "top_token_account_concentration"
        )
        assert stale_factor["status"] == "unknown"
        assert "top_token_account_concentration" in stale["data_completeness_missing"]


def test_deterministic_risk_marks_old_latest_observation_stale() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        chain = Chain(chain_name="Base", chain_slug="base", chain_id="8453")
        db.add(chain)
        db.flush()
        token = Token(chain_id=chain.id, contract_address="stale", normalized_address="stale")
        db.add(token)
        db.flush()
        db.add(MarketObservation(
            token_id=token.id,
            source="dexscreener",
            observed_at=now - timedelta(hours=13),
            market_cap=12_000,
            liquidity=8_000,
        ))
        db.commit()

        risk = build_risk_assessments(db, [token.id], Settings(), now)[token.id]
        freshness = next(factor for factor in risk["factors"] if factor["name"] == "market_data_freshness")

        assert freshness["status"] == "risk"
        assert freshness["source"] == "dexscreener"
        assert "old" in freshness["evidence"]


def test_discovery_persists_point_in_time_snapshots_and_horizon_outcomes() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as db:
        chain = Chain(chain_name="Base", chain_slug="base", chain_id="8453")
        db.add(chain)
        db.flush()
        token = Token(chain_id=chain.id, contract_address="outcome", normalized_address="outcome")
        db.add(token)
        db.flush()
        db.add(MarketObservation(
            token_id=token.id,
            source="dexscreener",
            market_cap=20_000,
            price=0.02,
            liquidity=10_000,
            volume=3_000,
        ))
        add_ai_baseline(db, token.id, 10_000)
        db.commit()

        service = AIDataService(Settings(ai_discovery_enabled=True), StubOllama(False))
        result = service.run_discovery(db, use_model=False)
        snapshot = db.query(AIDiscoverySnapshot).filter_by(token_id=token.id).one()
        assert snapshot.signal_score == result["candidates"][0]["score"]
        assert snapshot.run_id == result["candidates"][0]["run_id"]
        assert snapshot.risk_assessment["risk_score"] is not None

        captured_at = snapshot.captured_at
        if captured_at.tzinfo is None:
            captured_at = captured_at.replace(tzinfo=timezone.utc)
        snapshot.captured_at = captured_at - timedelta(hours=2)
        target_at = snapshot.captured_at + timedelta(hours=1)
        db.add_all([
            MarketObservation(
                token_id=token.id,
                source="dexscreener",
                observed_at=target_at + timedelta(minutes=5),
                market_cap=24_000,
            ),
            MarketObservation(
                token_id=token.id,
                source="dexscreener",
                observed_at=target_at - timedelta(minutes=5),
                market_cap=18_000,
            ),
        ])
        db.commit()

        service._record_due_outcomes(db, datetime.now(timezone.utc))
        db.commit()
        outcome = db.query(AIDiscoveryOutcome).filter_by(
            snapshot_id=snapshot.id,
            horizon_hours=1,
        ).one()

        assert outcome.status == "observed"
        assert outcome.market_cap == 24_000
        assert outcome.return_percent == 20
        assert outcome.max_return_percent == 20
        assert outcome.max_drawdown_percent == 10
        assert db.query(AIDiscoveryOutcome).filter_by(
            snapshot_id=snapshot.id,
            horizon_hours=1,
        ).count() == 1
        summary = service._outcome_summary(db)
        one_hour = next(item for item in summary if item["horizon_hours"] == 1)
        assert one_hour["observed_count"] == 1
        assert one_hour["average_return_percent"] == 20


def test_evaluation_compares_paired_signal_and_risk_adjusted_picks_without_rewriting_rank() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        chain = Chain(chain_name="Base", chain_slug="base", chain_id="8453")
        db.add(chain)
        db.flush()
        snapshots = []
        for token_id, (signal, risk, returned, rank) in enumerate(
            ((90, 100, 100, 1), (80, 0, 20, 2), (70, 0, -10, 3), (60, 0, 40, 4)),
            start=1,
        ):
            token = Token(
                chain_id=chain.id,
                contract_address=f"eval-{token_id}",
                normalized_address=f"eval-{token_id}",
            )
            db.add(token)
            db.flush()
            snapshot = AIDiscoverySnapshot(
                token_id=token.id,
                run_id="paired-run",
                captured_at=now - timedelta(hours=2),
                rank=rank,
                signal_score=signal,
                market_cap=10_000,
                risk_score=risk,
                data_completeness=100,
                risk_assessment={"risk_coverage_percent": 80},
            )
            db.add(snapshot)
            db.flush()
            snapshots.append(snapshot)
            db.add(AIDiscoveryOutcome(
                snapshot_id=snapshot.id,
                horizon_hours=1,
                target_at=now - timedelta(hours=1),
                observed_at=now - timedelta(minutes=55),
                status="observed",
                market_cap=10_000 * (1 + returned / 100),
                return_percent=returned,
                max_return_percent=max(0, returned),
                max_drawdown_percent=max(0, -returned),
            ))
        db.add(AIDiscoveryModelAssessment(
            snapshot_id=snapshots[0].id,
            model_name="test-model",
            score=75,
            assessment_hash="a" * 64,
            assessed_at=now - timedelta(hours=1, minutes=55),
        ))
        development_token = Token(
            chain_id=chain.id,
            contract_address="eval-development",
            normalized_address="eval-development",
        )
        db.add(development_token)
        db.flush()
        development_snapshot = AIDiscoverySnapshot(
            token_id=development_token.id,
            run_id="development-run",
            captured_at=now - timedelta(days=15),
            rank=1,
            signal_score=75,
            market_cap=10_000,
            risk_score=20,
            data_completeness=100,
            risk_assessment={"risk_coverage_percent": 80},
        )
        db.add(development_snapshot)
        db.flush()
        db.add(AIDiscoveryOutcome(
            snapshot_id=development_snapshot.id,
            horizon_hours=1,
            target_at=now - timedelta(days=15) + timedelta(hours=1),
            observed_at=now - timedelta(days=15) + timedelta(hours=1, minutes=5),
            status="observed",
            market_cap=11_000,
            return_percent=10,
            max_return_percent=10,
            max_drawdown_percent=0,
        ))
        db.commit()

        report = AIDataService(Settings(), StubOllama(False))._evaluation_report(db)
        one_hour = next(row for row in report["horizons"] if row["horizon_hours"] == 1)

        assert one_hour["signal_top3_risk_covered"]["average_return_percent"] == 36.67
        assert one_hour["risk_adjusted_top3"]["average_return_percent"] == 16.67
        assert one_hour["paired_run_count"] == 1
        assert one_hour["paired_mean_return_delta_percent"] == -20
        assert one_hour["ollama_reviewed"]["average_return_percent"] == 100
        assert one_hour["ollama_reviewed"]["average_model_score"] == 75
        assert one_hour["development"]["signal_top3_risk_covered"]["average_return_percent"] == 10
        assert one_hour["development"]["paired_run_count"] == 1
        assert one_hour["paired_return_delta_95_ci"]["lower_percent"] is None
        assert report["ranking_policy"]["risk_adjusted_status"] == "insufficient_evidence"
        assert report["ranking_policy"]["active_risk_weight"] == 0
        assert report["ranking_policy"]["active_ollama_weight"] == 0
        assert [snapshot.rank for snapshot in snapshots] == [1, 2, 3, 4]


def test_risk_adjusted_promotion_requires_full_independent_holdout_evidence() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        chain = Chain(chain_name="Base", chain_slug="base", chain_id="8453")
        db.add(chain)
        db.flush()
        token_data = (
            ("high-risk", 90, 100, -20, 30),
            ("signal-two", 80, 0, 10, 0),
            ("signal-three", 70, 0, 20, 0),
            ("adjusted-three", 60, 0, 15, 0),
        )
        tokens = []
        for address, _, _, _, _ in token_data:
            token = Token(
                chain_id=chain.id,
                contract_address=address,
                normalized_address=address,
            )
            db.add(token)
            db.flush()
            tokens.append(token)

        for day_offset in range(10, 0, -1):
            run_id = f"holdout-{day_offset}"
            capture_day = (now - timedelta(days=day_offset)).date()
            captured_at = datetime.combine(capture_day, datetime.min.time(), timezone.utc) + timedelta(minutes=10)
            for rank, (token, (_, signal, risk, returned, drawdown)) in enumerate(
                zip(tokens, token_data),
                start=1,
            ):
                snapshot = AIDiscoverySnapshot(
                    token_id=token.id,
                    run_id=run_id,
                    captured_at=captured_at,
                    rank=rank,
                    signal_score=signal,
                    market_cap=10_000,
                    risk_score=risk,
                    data_completeness=100,
                    risk_assessment={"risk_coverage_percent": 80},
                )
                db.add(snapshot)
                db.flush()
                for horizon in (1, 6, 24):
                    target_at = captured_at + timedelta(hours=horizon)
                    db.add(AIDiscoveryOutcome(
                        snapshot_id=snapshot.id,
                        horizon_hours=horizon,
                        target_at=target_at,
                        observed_at=target_at + timedelta(minutes=5),
                        status="observed",
                        market_cap=10_000 * (1 + returned / 100),
                        return_percent=returned,
                        max_return_percent=max(0, returned),
                        max_drawdown_percent=drawdown,
                    ))
            if day_offset == 10:
                duplicate_capture = captured_at + timedelta(minutes=30)
                for rank, (token, (_, signal, risk, _, _)) in enumerate(
                    zip(tokens, token_data),
                    start=1,
                ):
                    snapshot = AIDiscoverySnapshot(
                        token_id=token.id,
                        run_id="same-day-repeat",
                        captured_at=duplicate_capture,
                        rank=rank,
                        signal_score=signal,
                        market_cap=10_000,
                        risk_score=risk,
                        data_completeness=100,
                        risk_assessment={"risk_coverage_percent": 80},
                    )
                    db.add(snapshot)
                    db.flush()
                    for horizon in (1, 6, 24):
                        target_at = duplicate_capture + timedelta(hours=horizon)
                        db.add(AIDiscoveryOutcome(
                            snapshot_id=snapshot.id,
                            horizon_hours=horizon,
                            target_at=target_at,
                            observed_at=target_at + timedelta(minutes=5),
                            status="observed",
                            market_cap=1,
                            return_percent=-99,
                            max_return_percent=0,
                            max_drawdown_percent=99,
                        ))
        db.commit()

        report = AIDataService(Settings(), StubOllama(False))._evaluation_report(db)

        assert report["ranking_policy"]["risk_adjusted_status"] == "ready_for_manual_review"
        assert report["ranking_policy"]["active_risk_weight"] == 0
        assert report["ranking_policy"]["active_ollama_weight"] == 0
        assert report["ranking_policy"]["promotion_requires_manual_approval"] is True
        for horizon in report["horizons"]:
            holdout = horizon["holdout"]
            assert holdout["paired_run_count"] == 10
            assert holdout["paired_day_count"] == 10
            assert holdout["risk_adjusted_top3"]["average_return_percent"] == 15
            assert holdout["paired_return_delta_95_ci"]["lower_percent"] > 0
            assert holdout["paired_mean_max_drawdown_delta_percent"] < 0
        status = AIDataService(Settings(ai_discovery_enabled=False), StubOllama(False)).get_status(db)
        assert status["evaluation_report"]["ranking_policy"]["risk_adjusted_status"] == "ready_for_manual_review"