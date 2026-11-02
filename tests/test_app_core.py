from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.chains.registry import chain_registry
from app.chains.solana.adapter import SolanaAdapter
from app.config import Settings
from app.database import Base
from app.features.dashboard.service import get_overview
import app.features.security_analysis.service as security_analysis_service
from app.features.security_analysis.service import SecurityAnalysisService
from app.models import Chain, MarketObservation, RiskFinding, SecurityAnalysis, Token
from scripts.server import is_workspace_server, process_port
from app.workers.scheduler import RefreshScheduler


def test_settings_parse_helius_keys() -> None:
    settings = Settings(HELIUS_API_KEYS="key1,key2 , key3")
    assert settings.helius_keys == ["key1", "key2", "key3"]
    assert settings.token_refresh_interval_seconds == 300
    assert settings.ai_discovery_interval_seconds == 600


def test_file_logging_persists_messages_to_rotating_log(tmp_path: Path) -> None:
    from app.logging_config import configure_file_logging
    from logging.handlers import RotatingFileHandler

    log_path = tmp_path / "app.log"
    root_logger = logging.getLogger()
    server_loggers = [logging.getLogger(name) for name in ("uvicorn.error", "uvicorn.access")]
    prior_root_handlers = list(root_logger.handlers)
    prior_server_handlers = [list(logger.handlers) for logger in server_loggers]
    prior_propagate = [logger.propagate for logger in server_loggers]
    try:
        configure_file_logging("INFO", log_path)
        logging.getLogger("test.persistent").warning("persistent diagnostic marker")
        logging.getLogger("uvicorn.error").error("server diagnostic marker")
        for handler in root_logger.handlers:
            if isinstance(handler, RotatingFileHandler) and Path(handler.baseFilename) == log_path:
                handler.flush()
        contents = log_path.read_text(encoding="utf-8")
        assert "persistent diagnostic marker" in contents
        assert "server diagnostic marker" in contents
    finally:
        current_handlers = root_logger.handlers + [handler for logger in server_loggers for handler in logger.handlers]
        prior_handlers = prior_root_handlers + [handler for handlers in prior_server_handlers for handler in handlers]
        for handler in set(current_handlers) - set(prior_handlers):
            root_logger.removeHandler(handler)
            for logger in server_loggers:
                logger.removeHandler(handler)
            handler.close()
        for logger, propagate in zip(server_loggers, prior_propagate):
            logger.propagate = propagate


def test_registry_contains_supported_chains() -> None:
    chain_registry.register(SolanaAdapter())
    assert chain_registry.get("solana") is not None
    assert "solana" in chain_registry.list_enabled()


def test_refresh_scheduler_uses_default_minute_interval() -> None:
    scheduler = RefreshScheduler(refresh_interval_seconds=60)
    last_run = datetime(2026, 1, 1, 12, 0, 0)
    assert scheduler.is_due(last_run, now=datetime(2026, 1, 1, 12, 1, 0)) is True
    assert scheduler.is_due(last_run, now=datetime(2026, 1, 1, 12, 0, 30)) is False


def test_restart_launcher_only_matches_this_app_and_port() -> None:
    project_root = Path("/workspace/fomo").resolve()

    assert is_workspace_server(["python", "/workspace/fomo/main.py"], "/workspace/fomo", project_root)
    assert is_workspace_server(["uvicorn", "app.main:app"], "/workspace/fomo", project_root)
    assert not is_workspace_server(["python", "/workspace/other/main.py"], "/workspace/other", project_root)
    assert process_port(["uvicorn", "app.main:app", "--port", "9000"], {}, 8000) == 9000
    assert process_port(["python", "main.py"], {"PORT": "8000"}) == 8000


def test_token_identity_requires_chain_and_address() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        chain = Chain(chain_name="solana", chain_slug="solana")
        session.add(chain)
        session.commit()

        token_one = Token(chain_id=chain.id, contract_address="abc", normalized_address="abc")
        token_two = Token(chain_id=chain.id, contract_address="abc", normalized_address="abc")
        session.add_all([token_one, token_two])

        try:
            session.commit()
        except Exception:
            session.rollback()
            assert True
        else:
            assert False, "duplicate chain+address should violate uniqueness"


def test_overview_reports_total_tokens_separately_from_active_tokens() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        chain = Chain(chain_name="solana", chain_slug="solana")
        session.add(chain)
        session.flush()
        session.add_all([
            Token(chain_id=chain.id, contract_address="active", normalized_address="active"),
            Token(chain_id=chain.id, contract_address="graduated", normalized_address="graduated", is_graduated=True),
        ])
        session.commit()

        overview = get_overview(session)

        assert overview["tokens"] == 1
        assert overview["total_tokens"] == 2


def test_security_analysis_scores_market_and_risk_data() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        chain = Chain(chain_name="solana", chain_slug="solana")
        session.add(chain)
        session.flush()
        token = Token(
            chain_id=chain.id,
            contract_address="token-1",
            normalized_address="token-1",
            name="Alpha",
            ticker="ALP",
        )
        session.add(token)
        session.commit()

        session.add(
            MarketObservation(
                token_id=token.id,
                source="scheduled_refresh",
                market_cap=120_000.0,
                price=0.12,
                liquidity=600.0,
                volume=5000.0,
                is_valid=True,
                raw_data={"holder_count": 12, "pair_address": "pool-1", "dex_name": "raydium"},
            )
        )
        session.add(
            RiskFinding(
                token_id=token.id,
                severity="high",
                category="liquidity_risk",
                summary="Thin liquidity",
                confidence=0.8,
                confirmed=True,
            )
        )
        session.commit()

        result = SecurityAnalysisService().analyze_token(session, token.id)

        assert result["analysis_status"] == "ready"
        assert result["risk_level"] in {"medium", "high"}
        assert result["risk_score"] >= 30
        assert result["liquidity_security"]["status"] == "low_liquidity"
        assert result["analysis_metadata"]["phase"] == "phase_5"
        assert result["analysis_metadata"]["phases_completed"] == ["phase_1", "phase_2", "phase_3", "phase_4", "phase_5"]
        assert result["analysis_metadata"]["implementation_status"] == "complete_in_repo"
        assert "lock_verification" in result["liquidity_security"]
        assert "creator_wallet_cluster" in result
        assert "creator_activity" in result
        assert "tokens_sold_percent_supply" in result["creator_activity"]
        assert "combined_cluster_holdings" in result["holder_concentration"]
        assert "creator_linked_cluster_wallets" in result["holder_concentration"]


def test_security_analysis_keeps_holder_accounts_and_creator_linkage_separate(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        chain = Chain(chain_name="solana", chain_slug="solana")
        session.add(chain)
        session.flush()
        token = Token(
            chain_id=chain.id,
            contract_address="mint-verified",
            normalized_address="mint-verified",
        )
        session.add(token)
        session.flush()
        session.add(MarketObservation(
            token_id=token.id,
            source="dexscreener",
            market_cap=12_000,
            liquidity=8_000,
            raw_data={
                "locker_contract": "unverified-locker",
                "lock_expiration": "2099-01-01",
                "holder_count": 250,
            },
        ))
        session.commit()

        class FakeHeliusClient:
            def __init__(self, *args, **kwargs):
                pass

            def fetch_token_metadata(self, mint):
                return {"status": "ok", "metadata": {"supply": 1000, "decimals": 0}}

            def fetch_token_holders(self, mint, limit=20):
                return {
                    "status": "ok",
                    "provider": "helius",
                    "token_address": mint,
                    "holders": [
                        {"address": "token-account-1", "account_address": "token-account-1", "ui_amount": 200, "percentage_of_supply": 20},
                        {"address": "token-account-2", "account_address": "token-account-2", "ui_amount": 100, "percentage_of_supply": 10},
                    ],
                    "holder_count": None,
                    "returned_account_count": 2,
                    "top_5_percentage": 30,
                    "top_10_percentage": 30,
                    "concentration_status": "verified",
                    "concentration_basis": "top_token_accounts_as_share_of_total_supply",
                    "observed_at": datetime.now().astimezone().isoformat(),
                }

        monkeypatch.setattr(security_analysis_service, "HeliusClient", FakeHeliusClient)
        monkeypatch.setattr(
            security_analysis_service,
            "get_settings",
            lambda: Settings(helius_api_keys="test-key"),
        )

        result = SecurityAnalysisService().analyze_token(session, token.id)

        assert result["holder_concentration"]["top_10_percent"] == 30
        assert result["holder_concentration"]["top_10_source"] == "helius"
        assert result["holder_concentration"]["concentration_basis"] == "top_token_accounts_as_share_of_total_supply"
        assert result["creator_wallet_cluster"]["creator_wallets"] == []
        assert result["creator_wallet_cluster"]["status"] == "unknown"
        assert result["creator_activity"]["creator_address"] is None
        assert result["creator_activity"]["tokens_sold_percent_supply"] is None
        assert result["liquidity_security"]["lock_verification"]["status"] == "unknown"
        assert result["liquidity_security"]["lock_verification"]["lp_lock_verified"] is None


def test_security_analysis_uses_latest_saved_fomo_top_ten_holding() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        chain = Chain(chain_name="base", chain_slug="base")
        session.add(chain)
        session.flush()
        token = Token(
            chain_id=chain.id,
            contract_address="top-ten-token",
            normalized_address="top-ten-token",
        )
        session.add(token)
        session.flush()
        session.add_all([
            MarketObservation(
                token_id=token.id,
                source="fomo_family_metadata",
                raw_data={"fomo_metadata": {"top_10_holding_percent": 37.5}},
            ),
            MarketObservation(
                token_id=token.id,
                source="dexscreener",
                market_cap=25000,
                liquidity=8000,
                raw_data={},
            ),
            SecurityAnalysis(
                token_id=token.id,
                chain_name="base",
                token_address=token.normalized_address,
                risk_score=0,
                risk_level="unverified",
                analysis_status="ready",
                last_analyzed_at=datetime(2020, 1, 1),
                analysis_payload={},
            ),
        ])
        session.commit()

        result = SecurityAnalysisService().get_analysis(session, token.id)

        assert result["holder_concentration"]["top_10_percent"] == 37.5
        assert result["holder_concentration"]["top_10_source"] == "fomo_family"


def test_delete_token_removes_related_security_analysis() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        chain = Chain(chain_name="solana", chain_slug="solana")
        session.add(chain)
        session.flush()
        token = Token(
            chain_id=chain.id,
            contract_address="token-2",
            normalized_address="token-2",
            name="Bravo",
            ticker="BRV",
        )
        session.add(token)
        session.flush()
        session.add(
            SecurityAnalysis(
                token_id=token.id,
                chain_name="solana",
                token_address="token-2",
                risk_score=44,
                risk_level="medium",
                analysis_status="ready",
            )
        )
        session.commit()

        from app.features.tokens.service import delete_token

        token_id = token.id
        assert delete_token(session, token_id) is True
        session.commit()

        assert session.query(Token).filter(Token.id == token_id).count() == 0
        assert session.query(SecurityAnalysis).filter(SecurityAnalysis.token_id == token_id).count() == 0
