from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.chains.registry import chain_registry
from app.chains.solana.adapter import SolanaAdapter
from app.config import Settings
from app.database import Base
from app.features.dashboard.service import get_overview
from app.models import Chain, Token
from scripts.server import is_workspace_server, process_port
from app.workers.scheduler import RefreshScheduler


def test_settings_parse_helius_keys() -> None:
    settings = Settings(HELIUS_API_KEYS="key1,key2 , key3")
    assert settings.helius_keys == ["key1", "key2", "key3"]
    assert settings.token_refresh_interval_seconds == 300
    assert settings.ai_discovery_interval_seconds == 600


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
