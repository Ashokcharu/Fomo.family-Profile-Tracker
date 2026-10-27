from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.features.discovery.service import calculate_discovery_score, is_reeligible_after_dismissal
from app.features.graduated_tokens.service import create_graduation, graduation_gain_percent, record_graduation_progress, reconcile_graduation_eligibility, should_graduate
from app.features.market_data.service import market_cap_for_field, provider_priority_for_field
from app.integrations.dexscreener.client import DexScreenerClient, dexscreener_chain_name
from app.models import Chain, DiscoveryEvent, GraduationEvent, Token


def test_provider_priority_prefers_supported_field_sources() -> None:
    providers = {
        "market_cap": {
            "fomo_family": {"supported": False},
            "dexscreener": {"supported": True},
            "helius": {"supported": True},
        },
        "price": {
            "fomo_family": {"supported": True},
            "dexscreener": {"supported": False},
            "helius": {"supported": True},
        },
    }

    assert provider_priority_for_field("market_cap", providers) == "dexscreener"
    assert provider_priority_for_field("price", providers) == "fomo_family"


def test_dexscreener_metrics_use_matching_chain_and_social_link(monkeypatch) -> None:
    class Response:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {"pairs": [
                {
                    "chainId": "ethereum",
                    "baseToken": {"address": "token-address"},
                    "liquidity": {"usd": 90000},
                },
                {
                    "chainId": "solana",
                    "baseToken": {"address": "token-address", "name": "Test Token", "symbol": "TEST"},
                    "marketCap": 25000,
                    "priceUsd": "0.25",
                    "liquidity": {"usd": 8000},
                    "volume": {"h24": 1300},
                    "info": {
                        "imageUrl": "https://cdn.example/token.png",
                        "socials": [{"type": "twitter", "url": "https://x.com/token"}],
                    },
                },
            ]}

    monkeypatch.setattr("app.integrations.dexscreener.client.httpx.get", lambda *args, **kwargs: Response())

    metrics = DexScreenerClient().fetch_token_metrics("solana", "token-address")

    assert metrics is not None
    assert metrics["market_cap"] == 25000
    assert metrics["liquidity"] == 8000
    assert metrics["x_url"] == "https://x.com/token"
    assert metrics["name"] == "Test Token"
    assert metrics["symbol"] == "TEST"
    assert metrics["image_url"] == "https://cdn.example/token.png"
    assert dexscreener_chain_name("Fomo network 1399811149", "1399811149") == "solana"
    assert dexscreener_chain_name("Fomo network 4663", "4663") == "robinhood"


def test_dexscreener_batch_fetches_metrics_by_address(monkeypatch) -> None:
    requested_urls = []

    class Response:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> list[dict]:
            return [{
                "chainId": "solana",
                "baseToken": {"address": "token-address"},
                "marketCap": 12000,
                "liquidity": {"usd": 3000},
            }]

    def fake_get(url: str, **kwargs) -> Response:
        requested_urls.append(url)
        return Response()

    monkeypatch.setattr("app.integrations.dexscreener.client.httpx.get", fake_get)

    metrics = DexScreenerClient().fetch_token_metrics_batch("solana", ["token-address"])

    assert requested_urls == ["https://api.dexscreener.com/tokens/v1/solana/token-address"]
    assert metrics["token-address"]["market_cap"] == 12000


def test_dexscreener_does_not_treat_fdv_as_market_cap(monkeypatch) -> None:
    class Response:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {"pairs": [{
                "chainId": "solana",
                "baseToken": {"address": "token-address"},
                "fdv": 110061,
                "liquidity": {"usd": 175.19},
            }]}

    monkeypatch.setattr("app.integrations.dexscreener.client.httpx.get", lambda *args, **kwargs: Response())

    metrics = DexScreenerClient().fetch_token_metrics("solana", "token-address")

    assert metrics is not None
    assert metrics["market_cap"] is None
    assert metrics["fdv"] == 110061


def test_graduation_preserves_original_discovery_snapshot() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        chain = Chain(chain_name="solana", chain_slug="solana")
        session.add(chain)
        session.commit()

        token = Token(chain_id=chain.id, contract_address="0xabc", normalized_address="0xabc")
        session.add(token)
        session.commit()

        session.add(
            DiscoveryEvent(
                token_id=token.id,
                source="manual",
                initial_market_cap=10000,
                discovered_at=datetime.utcnow(),
            )
        )
        session.commit()

        event = create_graduation(session, token, 50000, 70000)
        assert event.market_cap_at_graduation == 70000
        assert session.query(DiscoveryEvent).filter_by(token_id=token.id).first().initial_market_cap == 10000
        assert should_graduate(70000, 50000) is True
        assert graduation_gain_percent(10000, 70000) == 600.0


def test_market_refresh_graduates_once_and_updates_peak() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        chain = Chain(chain_name="solana", chain_slug="solana")
        session.add(chain)
        session.flush()
        token = Token(chain_id=chain.id, contract_address="token", normalized_address="token")
        session.add(token)
        session.flush()

        assert record_graduation_progress(session, token, 50000, 49999) is None
        first_event = record_graduation_progress(
            session, token, 50000, 51000, liquidity_usd=5000, minimum_liquidity_usd=5000
        )
        session.flush()
        second_event = record_graduation_progress(
            session, token, 50000, 72000, liquidity_usd=5000, minimum_liquidity_usd=5000
        )
        session.commit()

        assert first_event is second_event
        assert token.is_graduated is True
        assert first_event.market_cap_at_graduation == 51000
        assert first_event.highest_observed_market_cap == 72000
        assert session.query(GraduationEvent).filter_by(token_id=token.id).count() == 1
        from app.features.tokens.service import active_token_count, list_tokens

        assert list_tokens(session) == []
        assert active_token_count(session) == 0


def test_low_liquidity_cannot_create_or_update_graduation() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        chain = Chain(chain_name="solana", chain_slug="solana")
        session.add(chain)
        session.flush()
        token = Token(chain_id=chain.id, contract_address="token", normalized_address="token")
        session.add(token)
        session.flush()

        assert record_graduation_progress(
            session,
            token,
            50000,
            110061,
            liquidity_usd=175.19,
            minimum_liquidity_usd=5000,
        ) is None
        assert session.query(GraduationEvent).filter_by(token_id=token.id).count() == 0
        assert token.is_graduated is False

        existing_event = create_graduation(session, token, 50000, 51000)
        assert record_graduation_progress(
            session,
            token,
            50000,
            110061,
            existing_event=existing_event,
            liquidity_usd=175.19,
            minimum_liquidity_usd=5000,
        ) is None
        assert existing_event.highest_observed_market_cap == 51000


def test_graduation_reconciliation_removes_only_ineligible_events() -> None:
    from app.models import DismissedToken, MarketObservation

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        chain = Chain(chain_name="Solana", chain_slug="solana")
        session.add(chain)
        session.flush()
        tracking_start = datetime(2026, 10, 21)
        invalid_token = Token(
            chain_id=chain.id,
            contract_address="first-seen-high",
            normalized_address="first-seen-high",
            first_seen_at=tracking_start,
            is_graduated=True,
        )
        high_rmc_token_without_event = Token(
            chain_id=chain.id,
            contract_address="high-rmc-no-event",
            normalized_address="high-rmc-no-event",
            first_seen_at=tracking_start,
        )
        eligible_token = Token(
            chain_id=chain.id,
            contract_address="grew-over-threshold",
            normalized_address="grew-over-threshold",
            first_seen_at=tracking_start,
            is_graduated=True,
        )
        session.add_all([invalid_token, high_rmc_token_without_event, eligible_token])
        session.flush()
        session.add_all([
            MarketObservation(
                token_id=invalid_token.id,
                source="scheduled_refresh",
                observed_at=datetime(2026, 10, 22),
                market_cap=75000,
                is_valid=True,
            ),
            MarketObservation(
                token_id=high_rmc_token_without_event.id,
                source="scheduled_refresh",
                observed_at=datetime(2026, 10, 22),
                market_cap=60000,
                is_valid=True,
            ),
            MarketObservation(
                token_id=eligible_token.id,
                source="scheduled_refresh",
                observed_at=datetime(2026, 10, 21, 1),
                market_cap=40000,
                is_valid=True,
            ),
            MarketObservation(
                token_id=eligible_token.id,
                source="scheduled_refresh",
                observed_at=datetime(2026, 10, 22),
                market_cap=75000,
                is_valid=True,
            ),
        ])
        session.add_all([
            GraduationEvent(
                token_id=invalid_token.id,
                graduated_at=datetime(2026, 10, 22),
                highest_observed_market_cap=75000,
                highest_observed_at=datetime(2026, 10, 22),
                threshold_usd=50000,
            ),
            GraduationEvent(
                token_id=eligible_token.id,
                graduated_at=datetime(2026, 10, 22),
                highest_observed_market_cap=75000,
                highest_observed_at=datetime(2026, 10, 22),
                threshold_usd=50000,
            ),
        ])
        session.commit()

        assert reconcile_graduation_eligibility(session, 50000) == 1
        assert invalid_token.is_graduated is False
        assert session.query(GraduationEvent).filter_by(token_id=invalid_token.id).count() == 0
        assert session.query(DismissedToken).filter_by(token_id=invalid_token.id, restored=False).count() == 1
        assert session.query(DismissedToken).filter_by(token_id=high_rmc_token_without_event.id, restored=False).count() == 1
        assert session.query(GraduationEvent).filter_by(token_id=high_rmc_token_without_event.id).count() == 0
        assert eligible_token.is_graduated is True
        assert session.query(GraduationEvent).filter_by(token_id=eligible_token.id).count() == 1


def test_scheduled_refresh_persists_market_and_activity_snapshot(monkeypatch) -> None:
    from app.features.market_data.service import list_market_history
    import app.workers.market_refresh as market_refresh
    from app.models import FollowedTrader, MarketObservation, TraderTransactionEvent

    class MetricsClient:
        def __init__(self, _base_url):
            pass

        def fetch_token_metrics_batch(self, _chain, addresses):
            return {
                address.lower(): {
                    "market_cap": 12000,
                    "price": 0.12,
                    "liquidity": 4500,
                    "volume": 900,
                    "name": "Example Token",
                    "symbol": "EX",
                    "holder_count": None,
                    "image_url": None,
                    "x_url": None,
                    "socials": [],
                    "raw_data": {},
                    "provider_name": "dexscreener",
                }
                for address in addresses
            }

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        chain = Chain(chain_name="solana", chain_slug="solana")
        session.add(chain)
        session.flush()
        token = Token(chain_id=chain.id, contract_address="token", normalized_address="token")
        trader = FollowedTrader(username="alice")
        session.add_all([token, trader])
        session.flush()
        session.add_all([
            TraderTransactionEvent(
                token_id=token.id,
                followed_trader_id=trader.id,
                wallet_address="wallet",
                event_type="buy",
                source="fomo_family",
            ),
            MarketObservation(
                token_id=token.id,
                source="fomo_family_metadata",
                observed_at=datetime(2026, 1, 1),
                is_valid=False,
                raw_data={"fomo_holder_count": 88},
            ),
        ])
        session.commit()

        monkeypatch.setattr(market_refresh, "SessionLocal", lambda: Session(engine))
        monkeypatch.setattr(market_refresh, "DexScreenerClient", MetricsClient)

        assert market_refresh.refresh_market_observations("https://example.invalid") == 1

        snapshot = (
            session.query(MarketObservation)
            .filter_by(token_id=token.id, source="scheduled_refresh")
            .one()
        )
        assert snapshot.market_cap == 12000
        assert snapshot.liquidity == 4500
        assert snapshot.raw_data["buyer_count"] == 1
        assert snapshot.raw_data["fomo_holder_count"] == 88
        assert "pair" not in snapshot.raw_data
        second_trader = FollowedTrader(username="bob")
        session.add(second_trader)
        session.flush()
        session.add(TraderTransactionEvent(
            token_id=token.id,
            followed_trader_id=second_trader.id,
            wallet_address="wallet-2",
            event_type="buy",
            source="fomo_family",
        ))
        session.commit()
        history = list_market_history(session, limit=1)
        assert history[0]["market_cap"] == 12000
        assert history[0]["liquidity"] == 4500
        assert history[0]["buyer_count"] == 1
        assert history[0]["fomo_holder_count"] == 88


def test_market_observation_compaction_preserves_recent_first_and_peak_rows() -> None:
    import app.workers.market_refresh as market_refresh
    from app.models import MarketObservation

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    now = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
    with Session(engine) as session:
        chain = Chain(chain_name="solana", chain_slug="solana")
        session.add(chain)
        session.flush()
        token = Token(chain_id=chain.id, contract_address="token", normalized_address="token")
        session.add(token)
        session.flush()
        observations = [
            MarketObservation(token_id=token.id, source="scheduled_refresh", observed_at=datetime(2026, 1, 1), market_cap=50),
            MarketObservation(token_id=token.id, source="scheduled_refresh", observed_at=datetime(2026, 1, 2), market_cap=80),
            MarketObservation(token_id=token.id, source="scheduled_refresh", observed_at=datetime(2026, 1, 3), market_cap=900),
            MarketObservation(token_id=token.id, source="scheduled_refresh", observed_at=datetime(2026, 10, 10, 8), market_cap=100),
            MarketObservation(token_id=token.id, source="scheduled_refresh", observed_at=datetime(2026, 10, 10, 8, 15), market_cap=500),
            MarketObservation(token_id=token.id, source="scheduled_refresh", observed_at=datetime(2026, 10, 10, 8, 30), market_cap=200),
            MarketObservation(token_id=token.id, source="scheduled_refresh", observed_at=now - timedelta(hours=2), market_cap=300),
            MarketObservation(token_id=token.id, source="scheduled_refresh", observed_at=now - timedelta(hours=1), market_cap=250),
            MarketObservation(token_id=token.id, source="fomo_family_metadata", observed_at=datetime(2026, 1, 2), market_cap=99, raw_data={"fomo_holder_count": 20}),
            MarketObservation(token_id=token.id, source="fomo_family_metadata", observed_at=datetime(2026, 10, 10, 8, 5), market_cap=100, raw_data={"fomo_holder_count": 22}),
            MarketObservation(token_id=token.id, source="fomo_family_metadata", observed_at=datetime(2026, 10, 10, 8, 20), market_cap=120, raw_data={"fomo_holder_count": 23}),
        ]
        session.add_all(observations)
        session.commit()

        removed = market_refresh.compact_market_observations(session, now=now)

        scheduled = session.query(MarketObservation).filter_by(
            token_id=token.id,
            source="scheduled_refresh",
        ).order_by(MarketObservation.observed_at).all()
        assert removed == 4
        assert [row.market_cap for row in scheduled] == [50, 900, 500, 200, 300, 250]
        fomo_rows = session.query(MarketObservation).filter_by(
            token_id=token.id,
            source="fomo_family_metadata",
        ).all()
        assert [row.market_cap for row in fomo_rows] == [120]


def test_scheduled_refresh_falls_back_for_tokens_missing_from_batch(monkeypatch) -> None:
    import app.workers.market_refresh as market_refresh
    from app.models import MarketObservation

    class MetricsClient:
        def __init__(self, _base_url):
            pass

        def fetch_token_metrics_batch(self, _chain, _addresses):
            return {}

        def fetch_token_metrics(self, _chain, address, _network_id):
            return {
                "market_cap": 12000,
                "liquidity": 4500,
                "raw_data": {},
                "provider_name": "dexscreener",
                "address": address,
            }

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        chain = Chain(chain_name="solana", chain_slug="solana")
        session.add(chain)
        session.flush()
        token = Token(chain_id=chain.id, contract_address="mint-address", normalized_address="mint-address")
        session.add(token)
        session.commit()

        monkeypatch.setattr(market_refresh, "SessionLocal", lambda: Session(engine))
        monkeypatch.setattr(market_refresh, "DexScreenerClient", MetricsClient)

        assert market_refresh.refresh_market_observations("https://example.invalid") == 1
        snapshot = session.query(MarketObservation).filter_by(token_id=token.id, source="scheduled_refresh").one()
        assert snapshot.market_cap == 12000
        assert snapshot.liquidity == 4500


def test_scheduled_refresh_uses_fresh_fomo_values_only_for_missing_dex_fields(monkeypatch) -> None:
    import app.workers.market_refresh as market_refresh
    from app.models import MarketObservation

    class MetricsClient:
        def __init__(self, _base_url):
            pass

        def fetch_token_metrics_batch(self, _chain, addresses):
            return {addresses[0].lower(): {
                "market_cap": 30000,
                "price": None,
                "liquidity": None,
                "volume": None,
                "provider_name": "dexscreener",
                "raw_data": {},
            }}

        def fetch_token_metrics(self, _chain, _address, _network_id):
            return None

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        chain = Chain(chain_name="solana", chain_slug="solana")
        session.add(chain)
        session.flush()
        token = Token(chain_id=chain.id, contract_address="mint", normalized_address="mint")
        session.add(token)
        session.flush()
        session.add(MarketObservation(
            token_id=token.id,
            source="fomo_family_metadata",
            provider_name="fomo_family",
            observed_at=datetime.now(),
            market_cap=20000,
            price=0.2,
            liquidity=5000,
            volume=700,
            is_valid=True,
        ))
        session.commit()

        monkeypatch.setattr(market_refresh, "SessionLocal", lambda: Session(engine))
        monkeypatch.setattr(market_refresh, "DexScreenerClient", MetricsClient)

        assert market_refresh.refresh_market_observations("https://example.invalid") == 1
        snapshot = session.query(MarketObservation).filter_by(token_id=token.id, source="scheduled_refresh").one()

        assert snapshot.market_cap == 30000
        assert snapshot.price == 0.2
        assert snapshot.liquidity == 5000
        assert snapshot.volume == 700
        assert snapshot.raw_data["fomo_fallback_fields"] == ["price", "liquidity", "volume"]


def test_scheduled_refresh_attributes_fomo_only_quotes_to_fomo(monkeypatch) -> None:
    import app.workers.market_refresh as market_refresh
    from app.models import MarketObservation

    class MetricsClient:
        def __init__(self, _base_url):
            pass

        def fetch_token_metrics_batch(self, _chain, _addresses):
            return {}

        def fetch_token_metrics(self, _chain, _address, _network_id):
            return None

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        chain = Chain(chain_name="solana", chain_slug="solana")
        session.add(chain)
        session.flush()
        token = Token(chain_id=chain.id, contract_address="mint", normalized_address="mint")
        session.add(token)
        session.flush()
        session.add(MarketObservation(
            token_id=token.id,
            source="fomo_family_metadata",
            observed_at=datetime.now(),
            market_cap=22000,
            is_valid=False,
        ))
        session.commit()

        monkeypatch.setattr(market_refresh, "SessionLocal", lambda: Session(engine))
        monkeypatch.setattr(market_refresh, "DexScreenerClient", MetricsClient)

        assert market_refresh.refresh_market_observations("https://example.invalid") == 1
        snapshot = session.query(MarketObservation).filter_by(token_id=token.id, source="scheduled_refresh").one()

        assert snapshot.provider_name == "fomo_family"
        assert snapshot.market_cap == 22000
        assert snapshot.raw_data["fomo_fallback_fields"] == ["market_cap"]


def test_scheduled_refresh_ignores_stale_fomo_quote_values(monkeypatch) -> None:
    import app.workers.market_refresh as market_refresh
    from app.models import MarketObservation

    class MetricsClient:
        def __init__(self, _base_url):
            pass

        def fetch_token_metrics_batch(self, _chain, _addresses):
            return {}

        def fetch_token_metrics(self, _chain, _address, _network_id):
            return None

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        chain = Chain(chain_name="solana", chain_slug="solana")
        session.add(chain)
        session.flush()
        token = Token(chain_id=chain.id, contract_address="mint", normalized_address="mint")
        session.add(token)
        session.flush()
        session.add(MarketObservation(
            token_id=token.id,
            source="fomo_family_metadata",
            observed_at=datetime(2020, 1, 1),
            market_cap=20000,
            is_valid=True,
        ))
        session.commit()

        monkeypatch.setattr(market_refresh, "SessionLocal", lambda: Session(engine))
        monkeypatch.setattr(market_refresh, "DexScreenerClient", MetricsClient)

        assert market_refresh.refresh_market_observations("https://example.invalid") == 0


def test_scheduled_refresh_does_not_store_empty_observations(monkeypatch) -> None:
    import app.workers.market_refresh as market_refresh
    from app.models import MarketObservation

    class MetricsClient:
        def __init__(self, _base_url):
            pass

        def fetch_token_metrics_batch(self, _chain, _addresses):
            return {}

        def fetch_token_metrics(self, _chain, _address, _network_id):
            return None

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        chain = Chain(chain_name="solana", chain_slug="solana")
        session.add(chain)
        session.flush()
        token = Token(chain_id=chain.id, contract_address="unlisted-mint", normalized_address="unlisted-mint")
        session.add(token)
        session.commit()

        monkeypatch.setattr(market_refresh, "SessionLocal", lambda: Session(engine))
        monkeypatch.setattr(market_refresh, "DexScreenerClient", MetricsClient)

        assert market_refresh.refresh_market_observations("https://example.invalid") == 0
        assert session.query(MarketObservation).filter_by(token_id=token.id).count() == 0


def test_latest_fomo_holder_count_uses_newest_observation_with_a_count() -> None:
    from app.models import MarketObservation
    from app.workers.market_refresh import _latest_fomo_holder_counts

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        chain = Chain(chain_name="solana", chain_slug="solana")
        session.add(chain)
        session.flush()
        token = Token(chain_id=chain.id, contract_address="mint", normalized_address="mint")
        session.add(token)
        session.flush()
        session.add_all([
            MarketObservation(
                token_id=token.id,
                source="test",
                observed_at=datetime(2026, 1, 1),
                raw_data={"fomo_holder_count": 88},
            ),
            MarketObservation(
                token_id=token.id,
                source="test",
                observed_at=datetime(2026, 1, 2),
                raw_data={},
            ),
        ])
        session.commit()

        assert _latest_fomo_holder_counts(session, [token.id]) == {token.id: 88}


def test_scheduled_refresh_limits_and_rotates_missing_token_fallbacks(monkeypatch) -> None:
    import app.workers.market_refresh as market_refresh

    class MetricsClient:
        fallback_addresses = []

        def __init__(self, _base_url):
            pass

        def fetch_token_metrics_batch(self, _chain, _addresses):
            return {}

        def fetch_token_metrics(self, _chain, address, _network_id):
            self.fallback_addresses.append(address)
            return None

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        chain = Chain(chain_name="solana", chain_slug="solana")
        session.add(chain)
        session.flush()
        session.add_all([
            Token(chain_id=chain.id, contract_address=f"mint-{index}", normalized_address=f"mint-{index}")
            for index in range(10)
        ])
        session.commit()

        monkeypatch.setattr(market_refresh, "SessionLocal", lambda: Session(engine))
        monkeypatch.setattr(market_refresh, "DexScreenerClient", MetricsClient)
        monkeypatch.setattr(market_refresh, "_fallback_cursor", 0)

        market_refresh.refresh_market_observations("https://example.invalid")
        first_batch = set(MetricsClient.fallback_addresses)
        assert len(first_batch) == market_refresh.FALLBACK_REQUEST_LIMIT

        MetricsClient.fallback_addresses.clear()
        market_refresh.refresh_market_observations("https://example.invalid")
        assert {"mint-8", "mint-9"}.issubset(MetricsClient.fallback_addresses)


def test_discovery_score_and_dismissal_filter() -> None:
    score = calculate_discovery_score({
        "unique_buyers": 2,
        "buy_cluster_window_seconds": 15 * 60,
        "market_cap": 20000,
        "liquidity": 7000,
        "age_minutes": 25,
        "freshness_seconds": 45,
        "risk": "low",
    })
    assert score >= 0
    assert is_reeligible_after_dismissal(False, "solana", "0xabc") is True
    assert is_reeligible_after_dismissal(True, "solana", "0xabc") is False


def test_scheduled_refresh_skips_dismissed_tokens(monkeypatch) -> None:
    import app.workers.market_refresh as market_refresh
    from app.models import DismissedToken, MarketObservation

    class MetricsClient:
        requested_addresses = []

        def __init__(self, _base_url):
            pass

        def fetch_token_metrics_batch(self, _chain, addresses):
            self.requested_addresses.extend(addresses)
            return {
                address.lower(): {
                    "market_cap": 1000,
                    "provider_name": "dexscreener",
                    "raw_data": {},
                }
                for address in addresses
            }

        def fetch_token_metrics(self, _chain, _address, _network_id):
            return None

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        chain = Chain(chain_name="solana", chain_slug="solana")
        session.add(chain)
        session.flush()
        active = Token(chain_id=chain.id, contract_address="active", normalized_address="active")
        dismissed = Token(chain_id=chain.id, contract_address="dismissed", normalized_address="dismissed")
        session.add_all([active, dismissed])
        session.flush()
        session.add(DismissedToken(token_id=dismissed.id, reason="Risk review", restored=False))
        session.commit()

        monkeypatch.setattr(market_refresh, "SessionLocal", lambda: Session(engine))
        monkeypatch.setattr(market_refresh, "DexScreenerClient", MetricsClient)

        assert market_refresh.refresh_market_observations("https://example.invalid") == 1
        assert MetricsClient.requested_addresses == ["active"]
        assert session.query(MarketObservation).filter_by(token_id=active.id).count() == 1
        assert session.query(MarketObservation).filter_by(token_id=dismissed.id).count() == 0


def test_scheduled_refresh_dismisses_token_first_quoted_above_threshold(monkeypatch) -> None:
    import app.workers.market_refresh as market_refresh
    from app.models import DismissedToken, MarketObservation, RiskFinding

    class MetricsClient:
        def __init__(self, _base_url):
            pass

        def fetch_token_metrics_batch(self, _chain, addresses):
            return {
                address.lower(): {
                    "market_cap": 75000,
                    "provider_name": "dexscreener",
                    "raw_data": {},
                }
                for address in addresses
            }

        def fetch_token_metrics(self, _chain, _address, _network_id):
            return None

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        chain = Chain(chain_name="solana", chain_slug="solana")
        session.add(chain)
        session.flush()
        token = Token(chain_id=chain.id, contract_address="already-large", normalized_address="already-large")
        session.add(token)
        session.flush()
        session.add(RiskFinding(
            token_id=token.id,
            severity="high",
            category="test",
            summary="risk finding",
        ))
        session.commit()

        monkeypatch.setattr(market_refresh, "SessionLocal", lambda: Session(engine))
        monkeypatch.setattr(market_refresh, "DexScreenerClient", MetricsClient)

        assert market_refresh.refresh_market_observations("https://example.invalid") == 0
        assert token.is_graduated is False
        assert session.query(GraduationEvent).filter_by(token_id=token.id).count() == 0
        assert session.query(RiskFinding).filter_by(token_id=token.id).count() == 0
        assert session.query(DismissedToken).filter_by(token_id=token.id, restored=False).count() == 1
        assert session.query(MarketObservation).filter_by(token_id=token.id).count() == 0
