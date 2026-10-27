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
from app.models import Chain, Token
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
