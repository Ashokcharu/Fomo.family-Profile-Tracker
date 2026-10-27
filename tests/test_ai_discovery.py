from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.features.ai_discovery.candidate_features import build_candidate_features
from app.features.ai_discovery import service as ai_discovery_service
from app.features.ai_discovery.service import AIDataService
from app.integrations.ollama.client import OllamaClient
from app.models import AIIntelligenceRecord, BackgroundJob, Chain, DismissedToken, Favorite, FollowedTrader, MarketObservation, Token, TraderTransactionEvent


class StubOllama:
    def __init__(self, available: bool, response: dict | None = None) -> None:
        self.available = available
        self.availability_checks = 0
        self.response = response or {"assessments": []}
        self.prompt = ""

    def is_available(self) -> bool:
        self.availability_checks += 1
        return self.available

    def generate(self, prompt: str) -> dict:
        self.prompt = prompt
        return {"status": "ok", "response": json.dumps(self.response)}


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
        records = db.query(AIIntelligenceRecord).filter_by(record_type="ai_discovery_candidate").all()
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
        assert datetime.fromisoformat(completed["candidates"][0]["ai_discovery_since"]).tzinfo == timezone.utc
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
        db.commit()

        service = AIDataService(Settings(ai_discovery_enabled=True), StubOllama(False))
        service.run_discovery(db, use_model=False)
        assert service.get_status(db)["candidate_count"] == 1

        assert service.remove_candidate(db, token.id) is True
        assert service.get_status(db)["candidate_count"] == 0
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


def test_candidate_replaced_by_a_better_ranked_token_is_archived(monkeypatch) -> None:
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
        db.commit()

        second_run = service.run_discovery(db, use_model=False)
        assert replacement.id in {candidate["token_id"] for candidate in second_run["candidates"]}
        assert tokens[0].id not in {candidate["token_id"] for candidate in second_run["candidates"]}
        analyzed = service.list_analyzed_candidates(db)
        replaced = next(candidate for candidate in analyzed if candidate["token_id"] == tokens[0].id)
        assert replaced["archive_reason"] == "replaced_by_better_candidates"
        assert replaced["score"] is not None


def test_low_signal_candidate_is_archived_as_failed(monkeypatch) -> None:
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
        db.commit()

        original_builder = ai_discovery_service.build_candidate_features

        def score_candidate_low(token_data, settings):
            features = original_builder(token_data, settings)
            features["heuristic_score"] = 40
            return features

        monkeypatch.setattr(ai_discovery_service, "build_candidate_features", score_candidate_low)
        service = AIDataService(Settings(ai_discovery_enabled=True), StubOllama(False))
        first_result = service.run_discovery(db, use_model=False)
        assert len(first_result["candidates"]) == 1

        result = service.run_discovery(db, use_model=False)

        assert result["candidates"] == []
        analyzed = service.list_analyzed_candidates(db)
        assert len(analyzed) == 1
        assert analyzed[0]["token_id"] == token.id
        assert analyzed[0]["archive_reason"] == "failed_low_signal"


def test_early_positive_movement_gets_a_bounded_momentum_signal() -> None:
    settings = Settings()
    early = build_candidate_features({
        "id": 1,
        "market_cap": 5_100,
        "liquidity": 5_000,
        "growth_multiple": 1.02,
    }, settings)
    already_grown = build_candidate_features({
        "id": 2,
        "market_cap": 10_000,
        "liquidity": 5_000,
        "growth_multiple": 2.0,
    }, settings)

    assert early["score_breakdown"]["early_momentum"] == 1.0
    assert early["score_breakdown"]["early_momentum"] <= 10
    assert already_grown["score_breakdown"]["early_momentum"] == 0


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
        original_ai_rmc = first["candidates"][0]["ai_rmc"]
        assert original_ai_rmc == 5_100
        assert first["candidates"][0]["ai_rh"] == original_ai_rmc

        service.record_feedback(db, token.id, "positive")
        db.add(MarketObservation(token_id=token.id, source="test", market_cap=5_200, liquidity=10_000))
        db.commit()
        second = service.run_discovery(db, use_model=False)
        assert second["candidates"][0]["ai_rmc"] == original_ai_rmc
        assert second["candidates"][0]["ai_rh"] == 5_200
        assert second["candidates"][0]["feedback"] == "positive"
        assert second["candidates"][0]["score"] == (
            second["candidates"][0]["heuristic_score"]
            + second["candidates"][0]["feedback_adjustment"]
            + second["candidates"][0]["continuation_adjustment"]
        )
        assert second["candidates"][0]["continuation_adjustment"] > 0
        assert len(second["top_picks"]) == 1
        assert second["top_picks"][0]["ai_rmc"] == original_ai_rmc
        assert second["top_picks"][0]["t_rmc"] == 5_200
        assert second["top_picks"][0]["t_rh"] == 5_200

        db.add(MarketObservation(token_id=token.id, source="test", market_cap=8_000, liquidity=10_000))
        db.commit()
        third = service.run_discovery(db, use_model=False)
        assert len(third["top_picks"]) == 1
        assert third["top_picks"][0]["ai_rmc"] == original_ai_rmc
        assert third["top_picks"][0]["ai_rh"] == 8_000
        assert third["top_picks"][0]["t_rmc"] == 5_200
        assert third["top_picks"][0]["t_rh"] == 8_000
        assert third["top_picks"][0]["ai_growth_multiple"] > 1

        db.add(MarketObservation(token_id=token.id, source="test", market_cap=9_000, liquidity=10_000))
        db.commit()
        refreshed_market = service.get_status(db)["top_picks"][0]
        assert refreshed_market["market_cap"] == 9_000
        assert refreshed_market["t_rmc"] == 5_200
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
        assert refreshed["candidates"][0]["ai_rh"] == 8_000
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
        db.add(baseline)
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