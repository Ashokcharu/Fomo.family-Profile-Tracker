from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.features.tokens.service import create_or_get_token, token_identity_key
from app.features.favorites.service import count_favorites, delete_favorite, favorite_token, list_favorites
from app.features.favorites.service import delete_favorites
from app.features.risk_analysis.service import count_risk_findings, delete_risk_findings, list_risk_findings, record_rmc_drawdown_risk
from app.features.dismissed_tokens.service import active_dismissed_token_ids, count_dismissed_tokens, delete_dismissed_tokens, dismiss_tokens, list_dismissed_tokens
from app.features.graduated_tokens.service import count_graduated_tokens, list_graduated_tokens
from app.features.tokens.service import active_token_count, delete_token, delete_tokens, list_active_token_ids, list_recent_bought_tokens, list_tokens, list_token_buyers
from app.models import Chain, DismissedToken, Favorite, FollowedTrader, GraduationEvent, MarketObservation, RiskFinding, Token, TraderTransactionEvent


def test_token_identity_key_uses_chain_and_normalized_address() -> None:
    assert token_identity_key("solana", "So11111111111111111111111111111111111111112") == (
        "solana",
        "so11111111111111111111111111111111111111112",
    )


def test_history_rows_can_be_bulk_deleted_without_deleting_tokens() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        chain = Chain(chain_name="Solana", chain_slug="solana")
        session.add(chain)
        session.commit()
        token = create_or_get_token(session, "solana", "token-address", chain_id=chain.id)
        favorite = Favorite(token_id=token.id)
        risk = RiskFinding(token_id=token.id, severity="low", category="test", summary="test finding")
        dismissed = DismissedToken(token_id=token.id, reason="test")
        session.add_all([favorite, risk, dismissed])
        session.commit()

        assert delete_favorites(session, [favorite.id]) == 1
        assert delete_risk_findings(session, [risk.id]) == 1
        assert delete_dismissed_tokens(session, [dismissed.id]) == 1
        assert session.query(Token).count() == 1


def test_tracked_tokens_excludes_tokens_in_other_active_lists() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        chain = Chain(chain_name="Solana", chain_slug="solana")
        session.add(chain)
        session.commit()
        tracked = create_or_get_token(session, "solana", "tracked", chain_id=chain.id)
        favorited = create_or_get_token(session, "solana", "favorited", chain_id=chain.id)
        graduated = create_or_get_token(session, "solana", "graduated", chain_id=chain.id)
        risk_tagged = create_or_get_token(session, "solana", "risk-tagged", chain_id=chain.id)
        dismissed = create_or_get_token(session, "solana", "dismissed", chain_id=chain.id)
        restored = create_or_get_token(session, "solana", "restored", chain_id=chain.id)
        graduated.is_graduated = True
        favorite_token(session, favorited)
        session.add_all([
            RiskFinding(token_id=risk_tagged.id, severity="low", category="test", summary="test finding"),
            DismissedToken(token_id=dismissed.id, reason="not a fit", restored=False),
            DismissedToken(token_id=restored.id, reason="reviewed", restored=True),
        ])
        session.commit()

        assert {row["id"] for row in list_tokens(session)} == {tracked.id, restored.id}
        assert list_active_token_ids(
            session,
            {tracked.id, favorited.id, graduated.id, risk_tagged.id, dismissed.id, restored.id},
        ) == {tracked.id, restored.id}


def test_active_token_id_lookup_preserves_duplicate_identity_rules() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        chain = Chain(chain_name="Base", chain_slug="base", chain_id="8453")
        alias_chain = Chain(chain_name="Fomo Network 8453", chain_slug="fomo-network-8453", chain_id="8453")
        session.add_all([chain, alias_chain])
        session.flush()
        first = Token(chain_id=chain.id, contract_address="same", normalized_address="same")
        second = Token(chain_id=alias_chain.id, contract_address="same", normalized_address="same")
        session.add(first)
        session.flush()
        session.add(second)
        session.commit()

        assert list_active_token_ids(session, {first.id}) == set()
        assert list_active_token_ids(session, {first.id, second.id}) == {second.id}

        session.add(Favorite(token_id=first.id))
        session.commit()
        assert list_active_token_ids(session, {first.id, second.id}) == set()


def test_tracked_token_search_filters_before_pagination() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        chain = Chain(chain_name="Solana", chain_slug="solana")
        session.add(chain)
        session.commit()
        first = create_or_get_token(session, "solana", "mint-one", chain_id=chain.id)
        second = create_or_get_token(session, "solana", "mint-two", chain_id=chain.id)
        create_or_get_token(session, "solana", "other-token", chain_id=chain.id)

        assert [row["id"] for row in list_tokens(session, limit=1, offset=0, search="mint")] == [second.id]
        assert [row["id"] for row in list_tokens(session, limit=1, offset=1, search="mint")] == [first.id]
        assert active_token_count(session, search="mint") == 2


def test_token_search_filters_all_token_history_tabs() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        chain = Chain(chain_name="Solana", chain_slug="solana")
        session.add(chain)
        session.flush()
        token = create_or_get_token(session, "solana", "needle-address", chain_id=chain.id, name="Needle Token")
        other = create_or_get_token(session, "solana", "other-address", chain_id=chain.id, name="Other Token")
        session.add_all([
            Favorite(token_id=token.id),
            GraduationEvent(token_id=token.id),
            RiskFinding(token_id=token.id, severity="low", category="test", summary="test"),
            DismissedToken(token_id=token.id, reason="test"),
            Favorite(token_id=other.id),
            GraduationEvent(token_id=other.id),
            RiskFinding(token_id=other.id, severity="low", category="test", summary="test"),
            DismissedToken(token_id=other.id, reason="test"),
        ])
        session.commit()

        assert count_favorites(session, "needle") == 1
        assert count_graduated_tokens(session, "needle") == 1
        assert count_risk_findings(session, "needle") == 1
        assert count_dismissed_tokens(session, "needle") == 1
        assert [row["contract_address"] for row in list_favorites(session, limit=1, search="needle")] == ["needle-address"]
        assert [row["contract_address"] for row in list_graduated_tokens(session, limit=1, search="needle")] == ["needle-address"]
        assert [row["contract_address"] for row in list_risk_findings(session, limit=1, search="needle")] == ["needle-address"]
        assert [row["contract_address"] for row in list_dismissed_tokens(session, limit=1, search="needle")] == ["needle-address"]


def test_create_or_get_token_prevents_duplicate_chain_identity() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        chain = Chain(chain_name="solana", chain_slug="solana")
        session.add(chain)
        session.commit()

        first = create_or_get_token(session, "solana", "0xabc", chain_id=chain.id)
        second = create_or_get_token(session, "solana", "0xABC", chain_id=chain.id)

        assert first.id == second.id
        assert session.query(type(first)).count() == 1


def test_token_rows_include_metrics_buyers_and_favorite_hides_them() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        chain = Chain(chain_name="Solana", chain_slug="solana")
        session.add(chain)
        session.commit()
        token = create_or_get_token(session, "solana", "token-address", chain_id=chain.id, ticker="TEST")
        trader = FollowedTrader(username="buyer-one")
        session.add(trader)
        session.commit()
        session.add(MarketObservation(
            token_id=token.id,
            source="dexscreener",
            market_cap=12000,
            liquidity=3000,
            raw_data={
                "holder_count": 41,
                "fomo_holder_count": 58,
                "fomo_url": "https://fomo.family/token/test",
                "socials": [{"type": "twitter", "url": "https://x.com/test"}],
                "pair": {
                    "baseToken": {"name": "Test Token", "symbol": "TEST"},
                    "info": {"imageUrl": "https://cdn.example/test.png", "socials": None},
                },
            },
        ))
        session.add(TraderTransactionEvent(
            token_id=token.id,
            followed_trader_id=trader.id,
            wallet_address="wallet-one",
            event_type="buy",
            source="fomo_family",
        ))
        session.commit()

        row = list_tokens(session)[0]
        assert row["chain_name"] == "Solana"
        assert row["market_cap"] == 12000
        assert row["liquidity"] == 3000
        assert row["first_seen_at"].endswith("+00:00")
        assert row["observed_at"].endswith("+00:00")
        assert row["fomo_holder_count"] == 58
        assert row["name"] == "Test Token"
        assert row["image_url"] == "https://cdn.example/test.png"
        assert row["buyer_count"] == 1
        assert row["x_url"] == "https://x.com/test"
        assert row["fomo_url"] == "https://fomo.family/tokens/solana/token-address"
        assert all(link["label"] != "Fomo" for link in row["social_links"])
        assert list_recent_bought_tokens(session)[0]["id"] == token.id
        assert list_token_buyers(session, token.id)[0]["username"] == "buyer-one"

        favorite_token(session, token, market_cap=row["market_cap"], liquidity=row["liquidity"])
        favorite_row = list_favorites(session)[0]
        assert favorite_row["contract_address"] == "token-address"
        assert favorite_row["fomo_url"] == "https://fomo.family/tokens/solana/token-address"
        assert favorite_row["market_cap"] == 12000
        assert favorite_row["fomo_holder_count"] == 58
        assert favorite_row["buyer_count"] == 1
        assert favorite_row["is_favorite"] is True

        session.add(GraduationEvent(
            token_id=token.id,
            market_cap_at_graduation=12000,
            threshold_usd=10000,
            highest_observed_market_cap=15000,
        ))
        session.commit()
        graduated_row = list_graduated_tokens(session)[0]
        assert graduated_row["market_cap"] == 12000
        assert graduated_row["fomo_holder_count"] == 58
        assert graduated_row["buyer_count"] == 1
        assert graduated_row["is_favorite"] is True

        assert list_tokens(session) == []
        assert session.query(Favorite).count() == 1
        assert delete_favorite(session, favorite_row["id"]) is True
        assert list_favorites(session) == []
        assert delete_favorite(session, favorite_row["id"]) is False


def test_recent_bought_tokens_are_unique_and_sorted_by_latest_buy() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        chain = Chain(chain_name="Solana", chain_slug="solana")
        session.add(chain)
        session.commit()
        first_token = create_or_get_token(session, "solana", "first-token", chain_id=chain.id)
        second_token = create_or_get_token(session, "solana", "second-token", chain_id=chain.id)
        first_buy = datetime(2026, 1, 1, 12, 0)
        session.add_all([
            TraderTransactionEvent(
                token_id=first_token.id,
                wallet_address="wallet-one",
                event_type="buy",
                event_time=first_buy,
                source="fomo_family",
            ),
            TraderTransactionEvent(
                token_id=second_token.id,
                wallet_address="wallet-two",
                event_type="buy",
                event_time=first_buy + timedelta(minutes=1),
                source="fomo_family",
            ),
            TraderTransactionEvent(
                token_id=first_token.id,
                wallet_address="wallet-three",
                event_type="buy",
                event_time=first_buy + timedelta(minutes=2),
                source="fomo_family",
            ),
        ])
        session.commit()

        assert [row["id"] for row in list_recent_bought_tokens(session, limit=1)] == [first_token.id]
        assert [row["id"] for row in list_recent_bought_tokens(session, limit=1, offset=1)] == [second_token.id]


def test_recent_bought_tokens_exclude_market_caps_at_or_above_threshold() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        chain = Chain(chain_name="Solana", chain_slug="solana")
        session.add(chain)
        session.commit()
        below = create_or_get_token(session, "solana", "below", chain_id=chain.id)
        at = create_or_get_token(session, "solana", "at", chain_id=chain.id)
        unknown = create_or_get_token(session, "solana", "unknown", chain_id=chain.id)
        event_time = datetime(2026, 1, 1, 12, 0)
        session.add_all([
            TraderTransactionEvent(token_id=below.id, wallet_address="one", event_type="buy", event_time=event_time, source="fomo_family"),
            TraderTransactionEvent(token_id=at.id, wallet_address="two", event_type="buy", event_time=event_time, source="fomo_family"),
            TraderTransactionEvent(token_id=unknown.id, wallet_address="three", event_type="buy", event_time=event_time, source="fomo_family"),
            MarketObservation(token_id=below.id, source="test", market_cap=49999),
            MarketObservation(token_id=at.id, source="test", market_cap=50000),
        ])
        session.commit()

        rows = list_recent_bought_tokens(session, discovery_threshold_usd=50000)
        assert [row["id"] for row in rows] == [below.id]


def test_tokens_dedupe_canonical_chain_identity_and_calculate_growth() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        solana = Chain(chain_name="Solana", chain_slug="solana")
        fomo_solana = Chain(chain_name="Fomo network 1399811149", chain_slug="fomo_network_1399811149", chain_id="1399811149")
        session.add_all([solana, fomo_solana])
        session.commit()
        session.add_all([
            Token(chain_id=solana.id, contract_address="TokenABC", normalized_address="tokenabc"),
            Token(chain_id=fomo_solana.id, contract_address="tokenabc", normalized_address="tokenabc"),
        ])
        session.commit()
        selected_token = session.query(Token).order_by(Token.id.desc()).first()
        first_time = datetime(2026, 1, 1)
        selected_token.first_seen_at = first_time
        session.add_all([
            MarketObservation(token_id=selected_token.id, source="test", market_cap=1000, observed_at=first_time),
            MarketObservation(token_id=selected_token.id, source="test", market_cap=3000, observed_at=first_time + timedelta(days=1)),
        ])
        session.commit()

        rows = list_tokens(session)

        assert active_token_count(session) == 1
        assert len(rows) == 1
        assert rows[0]["chain_name"] == "Solana"
        assert rows[0]["reach_market_cap"] == 1000
        assert rows[0]["reached_high_market_cap"] == 3000
        assert rows[0]["growth_percent"] == 200
        assert rows[0]["growth_multiple"] == 3


def test_reached_high_ignores_pre_tracking_and_invalid_market_values() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        chain = Chain(chain_name="Solana", chain_slug="solana")
        session.add(chain)
        session.flush()
        tracking_started = datetime(2026, 10, 21, 20, 45)
        token = Token(
            chain_id=chain.id,
            contract_address="peak-token",
            normalized_address="peak-token",
            first_seen_at=tracking_started,
        )
        session.add(token)
        session.flush()
        session.add_all([
            MarketObservation(
                token_id=token.id,
                source="scheduled_refresh",
                observed_at=datetime(2026, 10, 19, 20, 23),
                market_cap=16_940_993,
                is_valid=True,
            ),
            MarketObservation(
                token_id=token.id,
                source="fomo_family_metadata",
                observed_at=datetime(2026, 10, 22, 12, 0),
                market_cap=21_000_000_000,
                is_valid=False,
            ),
            MarketObservation(
                token_id=token.id,
                source="scheduled_refresh",
                observed_at=datetime(2026, 10, 22, 12, 0),
                market_cap=25_000,
                is_valid=True,
            ),
        ])
        session.add(GraduationEvent(
            token_id=token.id,
            graduated_at=datetime(2026, 10, 19, 20, 23),
            market_cap_at_graduation=4_053_006,
            threshold_usd=50_000,
            highest_observed_market_cap=16_940_993,
            highest_observed_at=datetime(2026, 10, 19, 20, 23),
        ))
        session.commit()

        row = list_tokens(session)[0]

        assert row["reached_high_market_cap"] == 25_000


def test_holder_count_uses_latest_available_provider_value() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        chain = Chain(chain_name="Solana", chain_slug="solana")
        session.add(chain)
        session.commit()
        token = create_or_get_token(session, "solana", "token-address", chain_id=chain.id)
        session.add_all([
            MarketObservation(
                token_id=token.id,
                source="dexscreener",
                observed_at=datetime(2026, 1, 1),
                raw_data={"pair": {"holderCount": 41}},
            ),
            MarketObservation(
                token_id=token.id,
                source="dexscreener",
                observed_at=datetime(2026, 1, 2),
                raw_data={"pair": {}},
            ),
            MarketObservation(
                token_id=token.id,
                source="fomo_family_metadata",
                observed_at=datetime(2026, 1, 3),
                is_valid=False,
                raw_data={"fomo_holder_count": 99},
            ),
        ])
        session.commit()

        assert list_tokens(session)[0]["fomo_holder_count"] == 99


def test_rmc_drawdown_moves_token_to_risk_and_records_once() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        chain = Chain(chain_name="Solana", chain_slug="solana")
        session.add(chain)
        session.commit()
        token = create_or_get_token(session, "solana", "drawdown-token", chain_id=chain.id)

        assert record_rmc_drawdown_risk(session, token, 100_000, 10_001) is None
        finding = record_rmc_drawdown_risk(session, token, 100_000, 10_000)
        session.commit()

        assert finding is not None
        assert finding.category == "rmc_drawdown"
        assert "90.0%" in finding.summary
        assert list_tokens(session) == []

        record_rmc_drawdown_risk(session, token, 100_000, 9_000)
        session.commit()
        assert session.query(RiskFinding).filter_by(token_id=token.id, category="rmc_drawdown").count() == 1


def test_unfavoriting_returns_token_to_tracked_list() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        chain = Chain(chain_name="Solana", chain_slug="solana")
        session.add(chain)
        session.commit()
        token = create_or_get_token(session, "solana", "favorite-token", chain_id=chain.id)

        favorite = favorite_token(session, token)
        assert favorite_token(session, token).id == favorite.id
        assert list_tokens(session) == []

        assert delete_favorite(session, favorite.id)
        assert [row["id"] for row in list_tokens(session)] == [token.id]


def test_delete_token_removes_dependent_records() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        chain = Chain(chain_name="Solana", chain_slug="solana")
        session.add(chain)
        session.commit()
        token = create_or_get_token(session, "solana", "token-address", chain_id=chain.id)
        session.add(MarketObservation(token_id=token.id, source="test", market_cap=1000))
        session.commit()

        assert delete_token(session, token.id) is True
        assert session.query(Token).count() == 0
        assert session.query(MarketObservation).count() == 0


def test_bulk_delete_removes_multiple_tokens_and_ignores_unknown_ids() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        chain = Chain(chain_name="Solana", chain_slug="solana")
        session.add(chain)
        session.commit()
        first = create_or_get_token(session, "solana", "first", chain_id=chain.id)
        second = create_or_get_token(session, "solana", "second", chain_id=chain.id)
        session.add_all([
            MarketObservation(token_id=first.id, source="test", market_cap=1000),
            MarketObservation(token_id=second.id, source="test", market_cap=2000),
        ])
        session.commit()

        assert delete_tokens(session, [first.id, second.id, 999999, first.id]) == 2
        assert session.query(Token).count() == 0
        assert session.query(MarketObservation).count() == 0


def test_token_without_provider_fomo_url_gets_family_link() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        solana = Chain(chain_name="Solana", chain_slug="solana")
        robinhood = Chain(chain_name="Robinhood Chain", chain_slug="robinhood_chain")
        session.add_all([solana, robinhood])
        session.commit()
        create_or_get_token(session, "solana", "TokenABC", chain_id=solana.id)
        create_or_get_token(session, "robinhood_chain", "0xe9e98bd5d254b821c22942101a00773c812ee12b", chain_id=robinhood.id)

        rows_by_address = {row["contract_address"]: row for row in list_tokens(session)}
        assert rows_by_address["TokenABC"]["fomo_url"] == "https://fomo.family/tokens/solana/TokenABC"
        assert rows_by_address["0xe9e98bd5d254b821c22942101a00773c812ee12b"]["fomo_url"] == (
            "https://fomo.family/tokens/robinhood/0xe9e98bd5d254b821c22942101a00773c812ee12b"
        )


def test_dismissing_risk_token_preserves_history_and_removes_active_findings() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        chain = Chain(chain_name="Solana", chain_slug="solana")
        session.add(chain)
        session.flush()
        token = create_or_get_token(session, "solana", "dismiss-me", chain_id=chain.id)
        finding = RiskFinding(
            token_id=token.id,
            severity="critical",
            category="test",
            summary="test risk",
        )
        observation = MarketObservation(token_id=token.id, source="test", market_cap=1000)
        session.add_all([finding, observation])
        second_token = create_or_get_token(session, "solana", "dismiss-me-too", chain_id=chain.id)
        session.add(RiskFinding(
            token_id=second_token.id,
            severity="high",
            category="test",
            summary="second risk",
        ))
        session.commit()

        result = dismiss_tokens(session, [token.id, second_token.id, token.id], reason="Risk review")

        assert result == {"dismissed": 2, "risk_findings_deleted": 2}
        assert session.query(Token).filter_by(id=token.id).one()
        assert session.query(MarketObservation).filter_by(token_id=token.id).count() == 1
        assert session.query(RiskFinding).filter_by(token_id=token.id).count() == 0
        assert session.query(RiskFinding).filter_by(token_id=second_token.id).count() == 0
        assert count_dismissed_tokens(session) == 2
        assert active_token_count(session) == 0
        assert active_dismissed_token_ids(session) == {token.id, second_token.id}

        assert dismiss_tokens(session, [token.id]) == {"dismissed": 0, "risk_findings_deleted": 0}
        assert count_dismissed_tokens(session) == 2
