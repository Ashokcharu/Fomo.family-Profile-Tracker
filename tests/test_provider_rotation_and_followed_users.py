from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.features.followed_users.routes import add_user
from app.features.followed_users.service import add_followed_user, delete_followed_user, delete_followed_users, followed_user_count, list_followed_users, normalize_followed_username, sync_followed_profiles
from app.integrations.fomo_family.client import FomoFamilyClient
from app.integrations.helius.client import HeliusClient
from app.models import Chain, FollowedTrader


def test_helius_keys_rotate_without_duplicates() -> None:
    client = HeliusClient(api_keys=["k1", "k2", "k3"], rotation_enabled=True)
    seen = {client.get_active_key() for _ in range(6)}
    assert seen == {"k1", "k2", "k3"}


def test_manual_followed_user_add_is_deduped() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        first = add_followed_user(session, "alice")
        second = add_followed_user(session, "alice")
        assert first.id == second.id
        assert session.query(FollowedTrader).count() == 1
        assert len(list_followed_users(session)) == 1
        assert first.profile_url == "https://fomo.family/profile/alice"
        assert list_followed_users(session)[0]["profile_url"] == "https://fomo.family/profile/alice"


def test_followed_users_deduplicate_case_insensitively() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        first = add_followed_user(session, "Alice")
        second = add_followed_user(session, "alice")

        assert first.id == second.id
        assert session.query(FollowedTrader).count() == 1


def test_followed_user_search_matches_username_and_platform_identifier() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        add_followed_user(session, "Alice", platform_identifier="profile-123")
        add_followed_user(session, "Bob", platform_identifier="profile-456")

        assert [row["username"] for row in list_followed_users(session, search="ali")] == ["Alice"]
        assert [row["username"] for row in list_followed_users(session, search="PROFILE-456")] == ["Bob"]
        assert followed_user_count(session, search="profile-") == 2
        assert followed_user_count(session, search="missing") == 0


def test_followed_profile_sync_updates_renamed_users_by_profile_id() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        existing = add_followed_user(
            session,
            "OldHandle",
            platform_identifier="fomo-profile-1",
            profile_url="https://fomo.family/profile/OldHandle",
        )
        existing.last_synced_at = datetime(2026, 10, 21)
        existing.sync_error = "old handle no longer resolves"
        session.commit()

        result = sync_followed_profiles(session, [{
            "profile_id": "fomo-profile-1",
            "username": "NewHandle",
            "profile_url": "https://fomo.family/profile/NewHandle",
        }, {
            "profile_id": "fomo-profile-2",
            "username": "NewPerson",
            "profile_url": "https://fomo.family/profile/NewPerson",
        }])

        assert result == {"added": 1, "updated": 1, "conflicts": 0}
        assert session.query(FollowedTrader).count() == 2
        renamed = session.query(FollowedTrader).filter_by(platform_identifier="fomo-profile-1").one()
        assert renamed.id == existing.id
        assert renamed.username == "NewHandle"
        assert renamed.last_synced_at is None
        assert renamed.sync_error is None


def test_manual_followed_user_duplicate_returns_explicit_conflict() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(fomo_activity_monitor=None)))
    with Session(engine) as session:
        add_followed_user(session, "NikolaTesla")

        with pytest.raises(HTTPException) as error:
            add_user(
                "https://fomo.family/profile/nikolatesla?r=0xNikolaTesla",
                request,
                session,
            )

        assert error.value.status_code == 409
        assert error.value.detail.startswith("Duplicate:")
        assert session.query(FollowedTrader).count() == 1


def test_followed_username_accepts_fomo_profile_url() -> None:
    assert normalize_followed_username(
        "https://fomo.family/profile/0xNikolaTesla?r=0xNikolaTesla"
    ) == "0xNikolaTesla"


def test_followed_user_can_be_deleted() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        trader = add_followed_user(session, "alice")

        assert delete_followed_user(session, trader.id) is True
        assert session.query(FollowedTrader).count() == 0


def test_followed_users_can_be_bulk_deleted() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        first = add_followed_user(session, "alice")
        second = add_followed_user(session, "bob")

        assert delete_followed_users(session, [first.id, second.id, first.id, 999999]) == 2
        assert session.query(FollowedTrader).count() == 0


def test_followed_user_sync_updates_platform_identity() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        trader = add_followed_user(session, "alice")
        updated = add_followed_user(
            session,
            "alice",
            platform_identifier="fomo-user-123",
            profile_url="https://fomo.family/profile/alice",
        )

        assert updated.id == trader.id
        assert updated.platform_identifier == "fomo-user-123"
        assert updated.profile_url == "https://fomo.family/profile/alice"
        assert session.query(FollowedTrader).count() == 1


def test_fomo_family_client_does_not_claim_sync_from_token_alone() -> None:
    for api_token in (None, "test-token"):
        result = FomoFamilyClient(api_token=api_token).sync_followed_users("0xNikolaTesla")

        assert result["status"] == "unavailable"
        assert result["manual_sync_required"] is True
        assert result["users"] == []


def test_trader_performance_keeps_relationship_baselines_and_mature_failures() -> None:
    from app.features.followed_users.performance import record_trader_token_pick, trader_performance_stats, update_token_pick_market_caps
    from app.models import Token, TraderTokenPick

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        chain = Chain(chain_name="Base", chain_slug="base", chain_id="8453")
        alice = FollowedTrader(username="alice")
        bob = FollowedTrader(username="bob")
        session.add_all([chain, alice, bob])
        session.flush()
        shared_token = Token(chain_id=chain.id, contract_address="shared", normalized_address="shared", ticker="SHARE")
        failed_token = Token(chain_id=chain.id, contract_address="failed", normalized_address="failed", ticker="FAIL")
        session.add_all([shared_token, failed_token])
        session.flush()

        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        record_trader_token_pick(session, alice.id, shared_token.id, 10_000, start)
        record_trader_token_pick(session, alice.id, shared_token.id, 30_000, start + timedelta(days=1))
        record_trader_token_pick(session, bob.id, shared_token.id, 20_000, start + timedelta(days=1))
        record_trader_token_pick(session, alice.id, failed_token.id, 5_000, start)
        session.commit()

        update_token_pick_market_caps(session, shared_token.id, 100_000, start + timedelta(days=2))
        update_token_pick_market_caps(session, failed_token.id, 400, start + timedelta(days=8))
        update_token_pick_market_caps(session, failed_token.id, 300, start + timedelta(days=9))
        session.commit()

        alice_stats = trader_performance_stats(session, [alice.id])[alice.id]
        bob_stats = trader_performance_stats(session, [bob.id])[bob.id]
        alice_picks = session.query(TraderTokenPick).filter_by(followed_trader_id=alice.id).all()
        sorted_traders = list_followed_users(session, sort_by="total_picks")

        assert len(alice_picks) == 2
        assert next(pick for pick in alice_picks if pick.token_id == shared_token.id).reached_market_cap == 10_000
        assert alice_stats["total_picks"] == 2
        assert alice_stats["success_rate_10x"] == 50
        assert alice_stats["average_growth"] == 5.5
        assert alice_stats["median_growth"] == 5.5
        assert alice_stats["best_pick"]["label"] == "SHARE"
        assert alice_stats["failure_rate"] == 100
        assert alice_stats["mature_pick_count"] == 1
        assert bob_stats["total_picks"] == 1
        assert bob_stats["success_rate_5x"] == 100
        assert bob_stats["failure_rate"] is None
        assert [trader["username"] for trader in sorted_traders] == ["alice", "bob"]


def test_trader_pick_backfill_handles_duplicate_observation_timestamps() -> None:
    from app.features.followed_users.performance import backfill_trader_token_picks
    from app.models import MarketObservation, Token, TraderTokenPick, TraderTransactionEvent

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        chain = Chain(chain_name="Base", chain_slug="base", chain_id="8453")
        alice = FollowedTrader(username="alice")
        bob = FollowedTrader(username="bob")
        session.add_all([chain, alice, bob])
        session.flush()
        token = Token(chain_id=chain.id, contract_address="shared", normalized_address="shared")
        session.add(token)
        session.flush()
        event_time = datetime(2026, 10, 20, tzinfo=timezone.utc)
        session.add_all([
            TraderTransactionEvent(
                token_id=token.id,
                followed_trader_id=trader.id,
                wallet_address=trader.username,
                chain_id=chain.id,
                event_type="buy",
                event_time=event_time,
                source="fomo_family",
            )
            for trader in (alice, bob)
        ])
        session.add_all([
            MarketObservation(
                token_id=token.id,
                source="fomo_family_discovery",
                observed_at=event_time,
                market_cap=12_000,
            )
            for _ in range(2)
        ])
        session.commit()

        assert backfill_trader_token_picks(session) == 2
        picks = session.query(TraderTokenPick).order_by(TraderTokenPick.followed_trader_id).all()
        assert [pick.reached_market_cap for pick in picks] == [12_000, 12_000]
        assert [pick.followed_trader_id for pick in picks] == [alice.id, bob.id]
