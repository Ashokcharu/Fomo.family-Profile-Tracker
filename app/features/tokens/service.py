from __future__ import annotations

from datetime import timezone
from urllib.parse import quote

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.chains import chain_registry
from app.integrations.dexscreener.client import dexscreener_chain_name
from app.models import (
    AIIntelligenceRecord,
    Chain,
    DismissedToken,
    DiscoveryEvent,
    Favorite,
    FollowedTrader,
    GraduationEvent,
    MarketObservation,
    RiskFinding,
    Token,
    TraderTokenPick,
    TraderTransactionEvent,
)


def token_identity_key(chain_name: str, address: str) -> tuple[str, str]:
    normalized_chain = (chain_name or "").strip().lower()
    adapter = chain_registry.get(normalized_chain)
    normalized_address = adapter.normalize_address(address) if adapter is not None else address.strip().lower()
    return normalized_chain, normalized_address


def _utc_isoformat(value) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat()


def filter_token_records(query, token_id_column, search: str | None):
    term = (search or "").strip()
    if not term:
        return query
    escaped_term = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped_term}%"
    return (
        query.join(Token, token_id_column == Token.id)
        .join(Chain, Token.chain_id == Chain.id)
        .filter(or_(
            Token.contract_address.ilike(pattern, escape="\\"),
            Token.normalized_address.ilike(pattern, escape="\\"),
            Token.name.ilike(pattern, escape="\\"),
            Token.ticker.ilike(pattern, escape="\\"),
            Chain.chain_name.ilike(pattern, escape="\\"),
        ))
    )


def create_or_get_token(
    db: Session,
    chain_name: str,
    address: str,
    *,
    chain_id: int | None = None,
    ticker: str | None = None,
    name: str | None = None,
) -> Token:
    normalized_chain, normalized_address = token_identity_key(chain_name, address)

    if chain_id is None:
        chain = db.query(Chain).filter(Chain.chain_slug == normalized_chain).first()
        if chain is None:
            chain = Chain(chain_name=normalized_chain, chain_slug=normalized_chain)
            db.add(chain)
            db.commit()
            db.refresh(chain)
        chain_id = chain.id

    existing = (
        db.query(Token)
        .filter(Token.chain_id == chain_id, Token.normalized_address == normalized_address)
        .first()
    )
    if existing is not None:
        return existing

    token = Token(
        chain_id=chain_id,
        contract_address=address.strip(),
        normalized_address=normalized_address,
        ticker=ticker,
        name=name,
    )
    db.add(token)
    db.commit()
    db.refresh(token)
    return token


def list_tokens(db: Session, limit: int = 20, offset: int = 0, search: str | None = None) -> list[dict]:
    return _token_data_for_rows(db, _active_token_rows(db, limit, offset, search))


def list_all_active_tokens(db: Session) -> list[dict]:
    return _token_data_for_rows(db, _active_token_rows(db, limit=None))


def list_recent_bought_tokens(
    db: Session,
    limit: int = 50,
    offset: int = 0,
    discovery_threshold_usd: float | None = None,
) -> list[dict]:
    latest_buys = _latest_fomo_buys(db)
    query = (
        db.query(Token, Chain.chain_name, Chain.chain_id)
        .join(Chain, Token.chain_id == Chain.id)
        .join(latest_buys, Token.id == latest_buys.c.token_id)
        .order_by(latest_buys.c.last_bought_at.desc(), Token.id.desc())
    )
    if discovery_threshold_usd is not None:
        query = query.filter(_latest_market_cap(db) < discovery_threshold_usd)
    rows = query.offset(offset).limit(limit).all()
    return _token_data_for_rows(db, rows)


def _latest_fomo_buys(db: Session):
    return (
        db.query(
            TraderTransactionEvent.token_id.label("token_id"),
            func.max(func.coalesce(
                TraderTransactionEvent.event_time,
                TraderTransactionEvent.detection_timestamp,
            )).label("last_bought_at"),
        )
        .filter(
            TraderTransactionEvent.source == "fomo_family",
            TraderTransactionEvent.event_type == "buy",
        )
        .group_by(TraderTransactionEvent.token_id)
        .subquery()
    )


def _latest_market_cap(db: Session):
    return (
        db.query(MarketObservation.market_cap)
        .filter(
            MarketObservation.token_id == Token.id,
            MarketObservation.is_valid.is_(True),
            MarketObservation.market_cap.is_not(None),
        )
        .order_by(MarketObservation.observed_at.desc(), MarketObservation.id.desc())
        .limit(1)
        .scalar_subquery()
    )


def _token_data_for_rows(db: Session, rows: list[tuple[Token, str, str | None]]) -> list[dict]:
    token_ids = [token.id for token, _, _ in rows]
    if not token_ids:
        return []

    latest_observations = _observations_by_time(db, token_ids, latest=True)
    holder_counts: dict[int, int] = {}
    fomo_holder_counts: dict[int, int] = {}
    holder_fields = ("fomo_holder_count", "holder_count", "holders_count", "holderCount", "holders")
    holder_expressions = [
        MarketObservation.raw_data[field].as_integer()
        for field in holder_fields
    ] + [
        MarketObservation.raw_data["pair"][field].as_integer()
        for field in holder_fields
    ] + [
        MarketObservation.raw_data["pair"]["info"][field].as_integer()
        for field in holder_fields
    ]
    for observation in (
        db.query(MarketObservation)
        .filter(
            MarketObservation.token_id.in_(token_ids),
            or_(*(field.is_not(None) for field in holder_expressions)),
        )
        .order_by(MarketObservation.observed_at.desc(), MarketObservation.id.desc())
        .all()
    ):
        payload = observation.raw_data if isinstance(observation.raw_data, dict) else {}
        holder_count = _holder_count(payload)
        if holder_count is not None:
            holder_counts.setdefault(observation.token_id, holder_count)
        fomo_holder_count = _fomo_holder_count(payload)
        if fomo_holder_count is not None:
            fomo_holder_counts.setdefault(observation.token_id, fomo_holder_count)

    first_observations = _observations_by_time(
        db,
        token_ids,
        latest=False,
        after_tracking_start=True,
    )

    initial_market_caps: dict[int, float] = {}
    for token_id, market_cap in (
        db.query(DiscoveryEvent.token_id, DiscoveryEvent.initial_market_cap)
        .join(Token, Token.id == DiscoveryEvent.token_id)
        .filter(DiscoveryEvent.token_id.in_(token_ids), DiscoveryEvent.initial_market_cap.is_not(None))
        .filter(DiscoveryEvent.discovered_at >= func.coalesce(Token.first_seen_at, Token.created_at))
        .order_by(DiscoveryEvent.discovered_at.asc(), DiscoveryEvent.id.asc())
        .all()
    ):
        initial_market_caps.setdefault(token_id, market_cap)

    peak_market_caps: dict[int, float] = {}
    tracking_starts = {
        token.id: token.first_seen_at or token.created_at
        for token, _, _ in rows
    }
    for token_id, market_cap, highest_observed_at, graduated_at in (
        db.query(
            GraduationEvent.token_id,
            GraduationEvent.highest_observed_market_cap,
            GraduationEvent.highest_observed_at,
            GraduationEvent.graduated_at,
        )
        .filter(GraduationEvent.token_id.in_(token_ids))
        .all()
    ):
        tracking_start = tracking_starts.get(token_id)
        peak_observed_at = highest_observed_at or graduated_at
        if (
            market_cap is not None
            and tracking_start is not None
            and peak_observed_at is not None
            and peak_observed_at >= tracking_start
        ):
            peak_market_caps[token_id] = max(peak_market_caps.get(token_id, market_cap), market_cap)
    for token_id, market_cap in (
        db.query(MarketObservation.token_id, func.max(MarketObservation.market_cap))
        .join(Token, Token.id == MarketObservation.token_id)
        .filter(MarketObservation.token_id.in_(token_ids), MarketObservation.market_cap.is_not(None))
        .filter(
            MarketObservation.is_valid.is_(True),
            MarketObservation.observed_at >= func.coalesce(Token.first_seen_at, Token.created_at),
        )
        .group_by(MarketObservation.token_id)
        .all()
    ):
        if market_cap is not None:
            peak_market_caps[token_id] = max(peak_market_caps.get(token_id, market_cap), market_cap)

    buyers: dict[int, dict[int, str]] = {}
    for token_id, trader_id, username in (
        db.query(TraderTransactionEvent.token_id, FollowedTrader.id, FollowedTrader.username)
        .join(FollowedTrader, TraderTransactionEvent.followed_trader_id == FollowedTrader.id)
        .filter(
            TraderTransactionEvent.token_id.in_(token_ids),
            TraderTransactionEvent.event_type == "buy",
        )
        .order_by(TraderTransactionEvent.event_time.desc(), TraderTransactionEvent.id.desc())
        .all()
    ):
        buyers.setdefault(token_id, {}).setdefault(trader_id, username)

    favorite_ids = {
        token_id for (token_id,) in db.query(Favorite.token_id)
        .filter(Favorite.token_id.in_(token_ids)).distinct().all()
    }
    result = []
    for token, chain_name, network_id in rows:
        observation = latest_observations.get(token.id)
        first_observation = first_observations.get(token.id)
        initial_market_cap = (
            first_observation.market_cap if first_observation and first_observation.market_cap is not None
            else initial_market_caps.get(token.id)
        )
        current_market_cap = observation.market_cap if observation else None
        peak_market_cap = peak_market_caps.get(token.id)
        growth_percent = None
        growth_multiple = None
        if initial_market_cap and current_market_cap is not None:
            growth_multiple = current_market_cap / initial_market_cap
            growth_percent = (growth_multiple - 1) * 100
        payload = observation.raw_data if observation and isinstance(observation.raw_data, dict) else {}
        provider_chain = dexscreener_chain_name(chain_name, network_id)
        display_chain = {
            "solana": "Solana",
            "ethereum": "Ethereum",
            "base": "Base",
            "bsc": "BNB Chain",
            "robinhood": "Robinhood",
        }.get(provider_chain, chain_name)
        result.append({
            "id": token.id,
            "chain_id": token.chain_id,
            "chain_name": display_chain,
            "contract_address": token.contract_address,
            "normalized_address": token.normalized_address,
            "ticker": token.ticker,
            "name": _token_name(token, payload),
            "image_url": _token_image_url(payload),
            "is_graduated": token.is_graduated,
            "is_favorite": token.id in favorite_ids,
            "market_cap": current_market_cap,
            "liquidity": observation.liquidity if observation else None,
            "fomo_holder_count": fomo_holder_counts.get(token.id, holder_counts.get(token.id)),
            "reach_market_cap": initial_market_cap,
            "reached_high_market_cap": peak_market_cap,
            "highest_observed_market_cap": peak_market_cap,
            "growth_percent": growth_percent,
            "growth_multiple": growth_multiple,
            "buyer_count": len(buyers.get(token.id, {})),
            "x_url": _x_url(payload),
            "social_links": (social_links := _social_links(payload, provider_chain, token.contract_address)),
            "fomo_url": _fomo_token_url(token.contract_address, provider_chain),
            "first_seen_at": _utc_isoformat(token.first_seen_at or token.created_at),
            "observed_at": _utc_isoformat(observation.observed_at) if observation else None,
        })
    return result


def token_display_data(db: Session, token_ids: list[int]) -> dict[int, dict]:
    unique_ids = set(token_ids)
    if not unique_ids:
        return {}

    token_rows = (
        db.query(Token, Chain.chain_name, Chain.chain_id)
        .join(Chain, Token.chain_id == Chain.id)
        .filter(Token.id.in_(unique_ids))
        .all()
    )
    return {
        row["id"]: {
            "token_id": row["id"],
            **{key: value for key, value in row.items() if key != "id"},
        }
        for row in _token_data_for_rows(db, token_rows)
    }


def _observations_by_time(
    db: Session,
    token_ids: list[int],
    *,
    latest: bool,
    after_tracking_start: bool = False,
) -> dict[int, MarketObservation]:
    if not token_ids:
        return {}
    time_aggregate = func.max if latest else func.min
    query = db.query(
        MarketObservation.token_id.label("token_id"),
        time_aggregate(MarketObservation.observed_at).label("observed_at"),
    ).filter(
        MarketObservation.token_id.in_(token_ids),
        MarketObservation.is_valid.is_(True),
    )
    if after_tracking_start:
        query = query.join(Token, Token.id == MarketObservation.token_id).filter(
            MarketObservation.observed_at >= func.coalesce(Token.first_seen_at, Token.created_at)
        )
    observation_times = query.group_by(MarketObservation.token_id).subquery()
    order = (MarketObservation.id.desc() if latest else MarketObservation.id.asc())
    observations = (
        db.query(MarketObservation)
        .join(
            observation_times,
            and_(
                MarketObservation.token_id == observation_times.c.token_id,
                MarketObservation.observed_at == observation_times.c.observed_at,
            ),
        )
        .order_by(order)
        .all()
    )
    result: dict[int, MarketObservation] = {}
    for observation in observations:
        result.setdefault(observation.token_id, observation)
    return result


def active_token_count(db: Session, search: str | None = None) -> int:
    return len(_active_token_rows(db, search=search))


def delete_token(db: Session, token_id: int) -> bool:
    return delete_tokens(db, [token_id]) > 0


def delete_tokens(db: Session, token_ids: list[int]) -> int:
    unique_ids = set(token_ids)
    existing_ids = {
        token_id for (token_id,) in db.query(Token.id).filter(Token.id.in_(unique_ids)).all()
    } if unique_ids else set()
    if not existing_ids:
        return 0
    for model in (
        AIIntelligenceRecord,
        DismissedToken,
        DiscoveryEvent,
        Favorite,
        GraduationEvent,
        MarketObservation,
        RiskFinding,
        TraderTokenPick,
        TraderTransactionEvent,
    ):
        db.query(model).filter(model.token_id.in_(existing_ids)).delete(synchronize_session=False)
    deleted = db.query(Token).filter(Token.id.in_(existing_ids)).delete(synchronize_session=False)
    db.commit()
    return deleted


def _active_token_rows(
    db: Session,
    limit: int | None = None,
    offset: int = 0,
    search: str | None = None,
) -> list[tuple[Token, str, str | None]]:
    favorite_ids = {token_id for (token_id,) in db.query(Favorite.token_id).distinct()}
    risk_finding_ids = {
        token_id for (token_id,) in db.query(RiskFinding.token_id)
        .distinct()
    }
    latest_dismissal_by_token: dict[int, bool] = {}
    for token_id, restored in (
        db.query(DismissedToken.token_id, DismissedToken.restored)
        .order_by(DismissedToken.dismissed_at.desc(), DismissedToken.id.desc())
        .all()
    ):
        latest_dismissal_by_token.setdefault(token_id, restored)
    dismissed_ids = {
        token_id for token_id, restored in latest_dismissal_by_token.items() if not restored
    }
    all_rows = (
        db.query(Token, Chain.chain_name, Chain.chain_id)
        .join(Chain, Token.chain_id == Chain.id)
        .order_by(Token.created_at.desc(), Token.id.desc())
        .all()
    )
    identities: dict[tuple[str, str], tuple[Token, str, str | None]] = {}
    favorite_identities = set()
    for token, chain_name, network_id in all_rows:
        if token.is_graduated or token.id in risk_finding_ids or token.id in dismissed_ids:
            continue
        identity = (dexscreener_chain_name(chain_name, network_id), token.normalized_address.lower())
        if token.id in favorite_ids:
            favorite_identities.add(identity)
        elif identity not in identities:
            identities[identity] = (token, chain_name, network_id)
    rows = [row for identity, row in identities.items() if identity not in favorite_identities]
    normalized_search = (search or "").strip().lower()
    if normalized_search:
        rows = [
            row for row in rows
            if any(normalized_search in str(value or "").lower() for value in (
                row[0].contract_address,
                row[0].normalized_address,
                row[0].name,
                row[0].ticker,
                row[1],
            ))
        ]
    return rows[max(0, offset):max(0, offset) + limit] if limit is not None else rows


def list_token_buyers(db: Session, token_id: int) -> list[dict]:
    rows = (
        db.query(TraderTransactionEvent, FollowedTrader.username, FollowedTrader.profile_url)
        .join(FollowedTrader, TraderTransactionEvent.followed_trader_id == FollowedTrader.id)
        .filter(TraderTransactionEvent.token_id == token_id, TraderTransactionEvent.event_type == "buy")
        .order_by(TraderTransactionEvent.event_time.desc(), TraderTransactionEvent.id.desc())
        .all()
    )
    latest_by_trader: dict[int, dict] = {}
    for event, username, profile_url in rows:
        latest_by_trader.setdefault(event.followed_trader_id, {
            "username": username,
            "profile_url": profile_url or f"https://fomo.family/profile/{quote(username, safe='')}",
            "event_time": event.event_time.isoformat() if event.event_time else None,
            "estimated_usd": event.estimated_usd,
            "tx_hash": event.tx_hash,
        })
    return list(latest_by_trader.values())


def _holder_count(payload: dict) -> int | None:
    sources = [payload]
    pair = payload.get("pair")
    if isinstance(pair, dict):
        sources.append(pair)
    for source in sources:
        for key in ("fomo_holder_count", "holder_count", "holders_count", "holderCount", "holders"):
            value = source.get(key)
            if isinstance(value, dict):
                value = value.get("count")
            if value is not None:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    continue
    return None


def _fomo_holder_count(payload: dict) -> int | None:
    metadata = payload.get("fomo_metadata") if isinstance(payload.get("fomo_metadata"), dict) else {}
    value = payload.get("fomo_holder_count", metadata.get("holder_count"))
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _token_name(token: Token, payload: dict) -> str | None:
    metadata = payload.get("fomo_metadata") if isinstance(payload.get("fomo_metadata"), dict) else {}
    pair = payload.get("pair") if isinstance(payload.get("pair"), dict) else payload
    base_token = pair.get("baseToken") if isinstance(pair.get("baseToken"), dict) else {}
    return (
        metadata.get("name")
        or base_token.get("name")
        or payload.get("name")
        or token.name
        or metadata.get("symbol")
        or base_token.get("symbol")
        or token.ticker
    )


def _token_image_url(payload: dict) -> str | None:
    metadata = payload.get("fomo_metadata") if isinstance(payload.get("fomo_metadata"), dict) else {}
    pair = payload.get("pair") if isinstance(payload.get("pair"), dict) else payload
    info = pair.get("info") if isinstance(pair.get("info"), dict) else {}
    candidates = (
        metadata.get("image_url"),
        payload.get("image_url"),
        info.get("imageUrl"),
        info.get("header"),
        pair.get("imageUrl"),
    )
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.startswith(("https://", "http://")):
            return candidate
    return None


def _x_url(payload: dict) -> str | None:
    for key in ("x_url", "twitter_url", "twitter"):
        value = payload.get(key)
        if isinstance(value, str) and value.startswith(("https://x.com/", "https://twitter.com/")):
            return value
    socials = payload.get("socials")
    if isinstance(socials, list):
        for social in socials:
            if not isinstance(social, dict):
                continue
            platform = str(social.get("type") or social.get("platform") or "").lower()
            url = social.get("url")
            if platform in {"twitter", "x"} and isinstance(url, str) and url.startswith(("https://x.com/", "https://twitter.com/")):
                return url
    return None


def _fomo_token_url(address: str, chain_name: str) -> str:
    chain_slugs = {"bsc": "bnb", "bnb_chain": "bnb", "robinhood_chain": "robinhood"}
    chain_slug = chain_slugs.get(chain_name.lower(), chain_name.lower().replace("_", "-"))
    return f"https://fomo.family/tokens/{quote(chain_slug, safe='')}/{quote(address, safe='')}"


def _social_links(payload: dict, chain_name: str, address: str) -> list[dict[str, str]]:
    pair = payload.get("pair") if isinstance(payload.get("pair"), dict) else {}
    info = pair.get("info") if isinstance(pair.get("info"), dict) else {}
    links: list[dict[str, str]] = []

    def add(label: str, url: object) -> None:
        if isinstance(url, str) and url.startswith(("https://", "http://")) and not any(item["url"] == url for item in links):
            links.append({"label": label, "url": url})

    add("DexScreener", pair.get("url"))
    add("X", payload.get("x_url"))
    socials = info.get("socials")
    if not isinstance(socials, list):
        socials = payload.get("socials")
    if not isinstance(socials, list):
        socials = []
    for item in socials:
        if not isinstance(item, dict):
            continue
        platform = str(item.get("type") or item.get("platform") or "").lower()
        add("X" if platform in {"x", "twitter"} else platform.title() or "Social", item.get("url"))
    websites = info.get("websites", payload.get("websites", []))
    if isinstance(websites, str):
        websites = [websites]
    for item in websites if isinstance(websites, list) else []:
        if isinstance(item, dict):
            add("Website", item.get("url"))
        else:
            add("Website", item)
    return links
