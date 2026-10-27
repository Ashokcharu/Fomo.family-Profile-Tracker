from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

import test as scraper
from app.integrations.fomo_family import monitor as fomo_monitor
from app.integrations.fomo_family.activity import parse_buy_swap, parse_profile_identity
from test import make_profile_record, normalize_profile_page_url, normalize_profile_url, profile_probe_succeeded
from app.features.followed_users.service import list_followed_buy_activity
from app.models import FollowedTrader, MarketObservation, Token, TraderTransactionEvent


def test_fomo_metadata_refresh_includes_tracked_tokens_outside_recent_swaps() -> None:
    class MetadataPage:
        async def evaluate(self, _script, token_ids):
            assert token_ids == ["recent-token:8453", "tracked-token:8453"]
            return [{
                "address": "tracked-token",
                "networkId": 8453,
                "token": {"info": {"holderCount": 120}},
            }]

    monitor = fomo_monitor.FomoFamilyActivityMonitor()
    monitor._page = MetadataPage()
    swap = {
        "id": "recent-swap",
        "display": {"side": "buy"},
        "outTokenAddress": "recent-token",
        "outNetworkId": 8453,
        "createdAt": "2026-10-18T10:47:10.000Z",
    }

    supplies, metadata = asyncio.run(
        monitor._fetch_token_supplies([swap], ["tracked-token:8453"])
    )

    assert supplies == {}
    assert metadata[("8453", "tracked-token")] == {"holder_count": 120}


def test_fomo_token_metadata_extracts_optional_quote_values() -> None:
    class MetadataPage:
        async def evaluate(self, _script, _token_ids):
            return [{
                "address": "quoted-token",
                "networkId": 8453,
                "token": {"info": {
                    "holderCount": 120,
                    "marketData": {
                        "marketCapUsd": "25000",
                        "priceUsd": "0.25",
                        "liquidity": {"usd": 8000},
                        "volume24h": "1300",
                    },
                }},
            }]

    monitor = fomo_monitor.FomoFamilyActivityMonitor()
    monitor._page = MetadataPage()

    _, metadata = asyncio.run(monitor._fetch_token_supplies([], ["quoted-token:8453"]))

    assert metadata[("8453", "quoted-token")] == {
        "holder_count": 120,
        "market_cap": 25000,
        "price": 0.25,
        "liquidity": 8000,
        "volume": 1300,
    }


def test_fomo_token_metadata_does_not_treat_fdv_as_market_cap() -> None:
    class MetadataPage:
        async def evaluate(self, _script, _token_ids):
            return [{
                "address": "fdv-only-token",
                "networkId": 8453,
                "token": {"info": {"marketData": {"fdv": "900000"}}},
            }]

    monitor = fomo_monitor.FomoFamilyActivityMonitor()
    monitor._page = MetadataPage()

    _, metadata = asyncio.run(monitor._fetch_token_supplies([], ["fdv-only-token:8453"]))

    assert metadata[("8453", "fdv-only-token")] == {}


def test_fomo_token_metadata_requests_are_batched(monkeypatch) -> None:
    class MetadataPage:
        def __init__(self):
            self.batches = []

        async def evaluate(self, _script, token_ids):
            self.batches.append(token_ids)
            return [
                {
                    "address": address,
                    "networkId": network_id,
                    "token": {"info": {"holderCount": index + 1}},
                }
                for index, (address, network_id) in enumerate(
                    token_id.split(":") for token_id in token_ids
                )
            ]

    monkeypatch.setattr(fomo_monitor, "FOMO_TOKEN_BATCH_SIZE", 2)
    monitor = fomo_monitor.FomoFamilyActivityMonitor()
    monitor._page = MetadataPage()
    token_ids = [f"token-{index}:4663" for index in range(5)]

    _, metadata = asyncio.run(monitor._fetch_token_supplies([], token_ids))

    assert [len(batch) for batch in monitor._page.batches] == [2, 2, 1]
    assert set(metadata) == {("4663", f"token-{index}") for index in range(5)}


def test_fomo_monitor_collects_followed_profiles_with_stable_ids() -> None:
    card = {
        "href": "/profile/new-handle?r=invite",
        "text": "New Handle\n@new-handle",
        "username": None,
        "profile_id": "fomo-profile-123",
    }

    class FakeLocator:
        first = None
        last = None

        def __init__(self, role):
            self.role = role
            self.first = self
            self.last = self

        async def wait_for(self, **_kwargs):
            pass

        async def click(self):
            pass

        async def count(self):
            return 1

        def locator(self, selector):
            return FakeLocator("scroll" if selector == ".overflow-y-auto" else "cards")

        async def evaluate(self, script):
            if "top:" in script:
                return {"top": 0, "height": 100, "client": 100}

        async def evaluate_all(self, _script):
            return [card]

    class FakePage:
        async def goto(self, *_args, **_kwargs):
            pass

        def get_by_role(self, *_args, **_kwargs):
            return FakeLocator("button")

        def locator(self, _selector):
            return FakeLocator("dialog")

        async def wait_for_timeout(self, _timeout):
            pass

    monitor = fomo_monitor.FomoFamilyActivityMonitor(
        profile_url="https://fomo.family/profile/0xNikolaTesla?r=0xNikolaTesla"
    )
    monitor._page = FakePage()

    profiles = asyncio.run(monitor._collect_profile_following())

    assert profiles == [{
        "profile_id": "fomo-profile-123",
        "username": "new-handle",
        "profile_url": "https://fomo.family/profile/new-handle",
    }]


def test_fomo_metadata_persists_quote_values_for_market_fallback() -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.database import Base
    from app.integrations.fomo_family.activity import persist_fomo_token_metadata
    from app.models import Chain

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        chain = Chain(chain_name="Base", chain_slug="base", chain_id="8453")
        session.add(chain)
        session.flush()
        token = Token(
            chain_id=chain.id,
            contract_address="quoted-token",
            normalized_address="quoted-token",
        )
        session.add(token)
        session.flush()

        persist_fomo_token_metadata(session, {("8453", "quoted-token"): {
            "market_cap": 25000,
            "price": 0.25,
            "liquidity": 8000,
            "volume": 1300,
        }})
        session.commit()

        observation = session.query(MarketObservation).one()
        assert observation.source == "fomo_family_metadata"
        assert observation.provider_name == "fomo_family"
        assert observation.is_valid is False
        assert (observation.market_cap, observation.price, observation.liquidity, observation.volume) == (
            25000,
            0.25,
            8000,
            1300,
        )


def test_fomo_metadata_refresh_runs_without_recent_trader_activity(monkeypatch) -> None:
    class MetadataPage:
        async def evaluate(self, _script, token_ids):
            assert token_ids == ["tracked-token:8453"]
            return [{
                "address": "tracked-token",
                "networkId": 8453,
                "token": {"info": {"holderCount": 120}},
            }]

    saved_metadata = {}
    monitor = fomo_monitor.FomoFamilyActivityMonitor()
    monitor._page = MetadataPage()
    monkeypatch.setattr(fomo_monitor, "_list_tracked_token_ids", lambda: ["tracked-token:8453"])
    monkeypatch.setattr(
        fomo_monitor,
        "_save_token_metadata",
        lambda metadata: saved_metadata.update(metadata) or len(metadata),
    )

    refreshed = asyncio.run(monitor._refresh_tracked_token_metadata())

    assert refreshed == 1
    assert saved_metadata[("8453", "tracked-token")] == {"holder_count": 120}


def test_fomo_metadata_refresh_maps_network_for_manual_chain_tokens(monkeypatch) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.database import Base
    from app.models import Chain, DismissedToken

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        chain = Chain(chain_name="solana", chain_slug="solana")
        session.add(chain)
        session.flush()
        active = Token(
            chain_id=chain.id,
            contract_address="manual-token",
            normalized_address="manual-token",
        )
        dismissed = Token(
            chain_id=chain.id,
            contract_address="dismissed-token",
            normalized_address="dismissed-token",
        )
        session.add_all([active, dismissed])
        session.flush()
        session.add(DismissedToken(token_id=dismissed.id, reason="risk", restored=False))
        session.commit()

        monkeypatch.setattr(fomo_monitor, "SessionLocal", lambda: Session(engine))

        assert fomo_monitor._list_tracked_token_ids() == ["manual-token:1399811149"]


def test_normalize_profile_url_removes_query_and_rejects_external_links() -> None:
    assert normalize_profile_url(
        "/profile/0xNikolaTesla?r=0xNikolaTesla",
        "https://fomo.family/following",
    ) == "https://fomo.family/profile/0xNikolaTesla"
    assert normalize_profile_url("https://example.com/profile/other", "https://fomo.family/following") is None
    assert normalize_profile_url("/settings", "https://fomo.family/following") is None


def test_normalize_profile_page_url_accepts_fomo_profile_and_rejects_other_routes() -> None:
    assert normalize_profile_page_url(
        "https://fomo.family/profile/0xNikolaTesla?r=AwareYappyGull"
    ) == "https://fomo.family/profile/0xNikolaTesla?r=AwareYappyGull"
    try:
        normalize_profile_page_url("https://fomo.family/discover")
    except ValueError:
        pass
    else:
        raise AssertionError("non-profile routes must be rejected")


def test_profile_record_keeps_username_separate_from_unknown_id() -> None:
    profile = make_profile_record(
        {
            "href": "/profile/0xNikolaTesla?r=invite",
            "text": "Nikola Tesla\n@TeslaTrades",
            "profile_id": None,
        },
        "https://fomo.family/following",
        collected_at="2026-10-18T00:00:00+00:00",
    )

    assert profile is not None
    assert profile["username"] == "TeslaTrades"
    assert profile["display_name"] == "Nikola Tesla"
    assert profile["profile_id"] is None
    assert profile["profile_url"] == "https://fomo.family/profile/0xNikolaTesla"


def test_profile_record_stores_explicit_dom_user_id() -> None:
    profile = make_profile_record(
        {
            "href": "/profile/tesla",
            "text": "@Tesla",
            "username": None,
            "display_name": None,
            "profile_id": "user-123",
        },
        "https://fomo.family/following",
    )

    assert profile is not None
    assert profile["username"] == "Tesla"
    assert profile["profile_id"] == "user-123"


def test_profile_probe_rejects_blank_or_marketing_page() -> None:
    profile_url = "https://fomo.family/profile/0xNikolaTesla"

    assert not profile_probe_succeeded(profile_url, "")
    assert not profile_probe_succeeded(profile_url, "where traders become legends")
    assert profile_probe_succeeded(profile_url, "Nikola Tesla\n@TeslaTrades\nFollowing")


def test_initialize_db_migrates_old_schema_without_losing_profiles(tmp_path, monkeypatch) -> None:
    database_path = tmp_path / "followers.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE followers (
                profile_url TEXT PRIMARY KEY,
                username TEXT,
                display_name TEXT,
                profile_text TEXT,
                collected_at TEXT
            )
            """
        )
        connection.execute(
            "INSERT INTO followers VALUES (?, ?, ?, ?, ?)",
            ("https://fomo.family/profile/alice", "alice", "Alice", "Alice", "old-time"),
        )

    monkeypatch.setattr(scraper, "DATA_DIR", tmp_path)
    monkeypatch.setattr(scraper, "DB_PATH", database_path)
    scraper.initialize_db()

    with sqlite3.connect(database_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(followers)")}
        saved_profile = connection.execute("SELECT username, collected_at FROM followers").fetchone()

    assert "profile_id" in columns
    assert saved_profile == ("alice", "old-time")


def test_monitor_prioritizes_unsynced_and_only_requeues_due_traders(monkeypatch) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.database import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine)
    monkeypatch.setattr(fomo_monitor, "SessionLocal", session_factory)
    now = datetime.now(timezone.utc)

    with session_factory() as session:
        session.add_all(
            [
                FollowedTrader(username="older-unsynced"),
                FollowedTrader(username="newer-unsynced"),
                FollowedTrader(username="due", last_synced_at=now - timedelta(minutes=10)),
                FollowedTrader(username="recent", last_synced_at=now - timedelta(minutes=1)),
                FollowedTrader(username="paused", tracking_enabled=False),
            ]
        )
        session.commit()

    assert [row["username"] for row in fomo_monitor._list_traders(300)] == [
        "newer-unsynced",
        "older-unsynced",
        "due",
    ]


def test_save_profiles_persists_username_and_explicit_profile_id(tmp_path, monkeypatch) -> None:
    database_path = tmp_path / "followers.db"
    monkeypatch.setattr(scraper, "DATA_DIR", tmp_path)
    monkeypatch.setattr(scraper, "DB_PATH", database_path)
    monkeypatch.setattr(scraper, "JSON_PATH", tmp_path / "followers.json")
    profile = make_profile_record(
        {"href": "/profile/alice", "text": "Alice\n@alice", "profile_id": "fomo-123"},
        "https://fomo.family/following",
    )

    assert profile is not None
    scraper.save_profiles([profile])

    with sqlite3.connect(database_path) as connection:
        saved_profile = connection.execute(
            "SELECT profile_id, username FROM followers"
        ).fetchone()
    assert saved_profile == ("fomo-123", "alice")
    assert (tmp_path / "followers.json").exists()


def test_fomo_profile_identity_requires_stable_matching_handle() -> None:
    payload = {"responseObject": {"id": "user-123", "userHandle": "RunningClam", "address": "wallet"}}

    assert parse_profile_identity(payload, "runningclam") == {
        "profile_id": "user-123",
        "username": "RunningClam",
        "wallet_address": "wallet",
    }
    assert parse_profile_identity({"responseObject": {"id": "user-123"}}, "runningclam") is None


def test_fomo_swap_parser_keeps_only_identifiable_buys() -> None:
    swap = {
        "id": "swap-123",
        "display": {"side": "buy"},
        "outTokenAddress": "token-address",
        "outNetworkId": 8453,
        "outHumanAmount": 42.5,
        "humanUsdAmountIn": 12.75,
        "displayPriceUsd": 0.3,
        "createdAt": "2026-10-18T10:47:10.000Z",
    }

    parsed = parse_buy_swap(swap)

    assert parsed is not None
    assert parsed["swap_id"] == "swap-123"
    assert parsed["token_address"] == "token-address"
    assert parsed["network_id"] == "8453"
    assert parsed["token_amount"] == 42.5
    assert parsed["estimated_usd"] == 12.75
    assert parsed["market_cap"] is None
    assert parsed["event_time"].isoformat() == "2026-10-18T10:47:10+00:00"
    assert parse_buy_swap({**swap, "display": {"side": "sell"}}) is None
    assert parse_buy_swap({**swap, "id": None}) is None


def test_fomo_swap_parser_does_not_treat_fdv_as_market_cap() -> None:
    swap = {
        "id": "swap-fdv",
        "display": {"side": "buy", "fdv": "900000"},
        "outTokenAddress": "fdv-only-token",
        "outNetworkId": 8453,
        "createdAt": "2026-10-18T10:47:10.000Z",
    }

    parsed = parse_buy_swap(swap)

    assert parsed is not None
    assert parsed["market_cap"] is None


def test_fomo_buy_persistence_deduplicates_by_provider_swap_id() -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.database import Base
    from app.integrations.fomo_family.activity import persist_fomo_buys

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    swap = {
        "id": "swap-123",
        "display": {"side": "buy"},
        "outTokenAddress": "token-address",
        "outNetworkId": 8453,
        "outHumanAmount": 42.5,
        "humanUsdAmountIn": 12.75,
        "displayPriceUsd": 0.3,
        "createdAt": "2026-10-18T10:47:10.000Z",
    }

    with Session(engine) as session:
        trader = FollowedTrader(username="alice")
        session.add(trader)
        session.flush()

        metadata = {("8453", "token-address"): {
            "holder_count": 88,
            "name": "Fomo Token",
            "symbol": "FOMO",
            "image_url": "https://cdn.example/fomo.png",
        }}
        assert persist_fomo_buys(
            session,
            trader,
            "wallet",
            [swap, {**swap, "display": {"side": "sell"}}],
            discovery_threshold_usd=50000,
            market_caps={("8453", "token-address"): 12000},
            token_metadata=metadata,
        ) == 1
        session.commit()
        assert persist_fomo_buys(session, trader, "wallet", [swap]) == 0

        event = session.query(TraderTransactionEvent).one()
        token = session.query(Token).one()
        observation = session.query(MarketObservation).one()
        assert event.dedup_key == "fomo:swap-123"
        assert event.event_type == "buy"
        assert event.estimated_usd == 12.75
        assert token.contract_address == "token-address"
        assert token.name == "Fomo Token"
        assert token.ticker == "FOMO"
        assert token.first_seen_at is not None
        assert observation.raw_data["fomo_holder_count"] == 88
        assert observation.raw_data["image_url"] == "https://cdn.example/fomo.png"
        assert list_followed_buy_activity(session)[0]["username"] == "alice"


def test_fomo_buy_persistence_stores_only_known_market_caps_below_threshold() -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.database import Base
    from app.integrations.fomo_family.activity import persist_fomo_buys
    from app.models import Chain, FollowedTrader, MarketObservation, Token, TraderTransactionEvent

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    base_swap = {
        "display": {"side": "buy"},
        "outNetworkId": 8453,
        "outHumanAmount": 42.5,
        "humanUsdAmountIn": 12.75,
        "displayPriceUsd": 0.3,
        "createdAt": "2026-10-18T10:47:10.000Z",
    }
    swaps = [
        {**base_swap, "id": "below-threshold", "outTokenAddress": "below"},
        {**base_swap, "id": "at-threshold", "outTokenAddress": "at"},
        {**base_swap, "id": "unknown-market-cap", "outTokenAddress": "unknown"},
    ]

    with Session(engine) as session:
        chain = Chain(chain_name="Base", chain_slug="base", chain_id="8453")
        trader = FollowedTrader(username="alice")
        session.add_all([chain, trader])
        session.flush()

        inserted = persist_fomo_buys(
            session,
            trader,
            "wallet",
            swaps,
            discovery_threshold_usd=50000,
            market_caps={("8453", "below"): 49999, ("8453", "at"): 50000},
        )
        session.commit()

        assert inserted == 1
        assert session.query(Token).count() == 1
        assert session.query(TraderTransactionEvent).count() == 1
        assert session.query(MarketObservation).one().market_cap == 49999


def test_fomo_buy_market_cap_can_be_derived_from_price_and_circulating_supply() -> None:
    from app.integrations.fomo_family.activity import fetch_fomo_buy_market_caps

    swap = {
        "id": "swap-with-token-supply",
        "display": {"side": "buy"},
        "outTokenAddress": "0x108df161d367058011443a6792660ad07dd399ed",
        "outNetworkId": 4663,
        "displayPriceUsd": 1.12348e-05,
        "createdAt": "2026-10-18T10:47:10.000Z",
    }

    market_caps = fetch_fomo_buy_market_caps(
        [swap],
        token_supplies={("4663", "0x108df161d367058011443a6792660ad07dd399ed"): 982476945.859445},
    )

    assert market_caps[("4663", "0x108df161d367058011443a6792660ad07dd399ed")] == 11037.931991341691