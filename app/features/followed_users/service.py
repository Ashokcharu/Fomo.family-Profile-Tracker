from __future__ import annotations

from urllib.parse import quote, unquote, urlparse

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.features.tokens.service import token_display_data
from app.features.followed_users.performance import trader_performance_stats
from app.models import Chain, FollowedTrader, FollowedWalletAssociation, Token, TraderTokenPick, TraderTransactionEvent


def normalize_followed_username(value: str) -> str:
    normalized = (value or "").strip()
    if "://" in normalized:
        parsed = urlparse(normalized)
        parts = [part for part in parsed.path.split("/") if part]
        if (
            parsed.scheme != "https"
            or (parsed.hostname or "").lower() not in {"fomo.family", "www.fomo.family"}
            or len(parts) != 2
            or parts[0].lower() != "profile"
        ):
            raise ValueError("Enter a username or an HTTPS fomo.family/profile URL.")
        normalized = unquote(parts[1])
    normalized = normalized.strip().lstrip("@").strip()
    if not normalized:
        raise ValueError("username is required")
    return normalized


def add_followed_user(db: Session, username: str, platform_identifier: str | None = None, profile_url: str | None = None) -> FollowedTrader:
    normalized = normalize_followed_username(username)

    existing = db.query(FollowedTrader).filter(func.lower(FollowedTrader.username) == normalized.lower()).first()
    if existing is not None:
        changed = False
        if platform_identifier and existing.platform_identifier != platform_identifier:
            existing.platform_identifier = platform_identifier
            changed = True
        if profile_url and existing.profile_url != profile_url:
            existing.profile_url = profile_url
            changed = True
        if changed:
            db.commit()
            db.refresh(existing)
        return existing

    row = FollowedTrader(
        username=normalized,
        platform_identifier=platform_identifier,
        profile_url=profile_url or f"https://fomo.family/profile/{quote(normalized, safe='')}",
        tracking_enabled=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def sync_followed_profiles(db: Session, profiles: list[dict[str, str | None]]) -> dict[str, int]:
    added = 0
    updated = 0
    conflicts = 0
    for profile in profiles:
        username = normalize_followed_username(profile.get("username") or "")
        profile_id = (profile.get("profile_id") or "").strip() or None
        profile_url = profile.get("profile_url") or f"https://fomo.family/profile/{quote(username, safe='')}"
        existing_by_id = (
            db.query(FollowedTrader)
            .filter(FollowedTrader.platform_identifier == profile_id)
            .first()
            if profile_id
            else None
        )
        existing_by_username = (
            db.query(FollowedTrader)
            .filter(func.lower(FollowedTrader.username) == username.lower())
            .first()
        )
        if existing_by_id and existing_by_username and existing_by_id.id != existing_by_username.id:
            conflicts += 1
            continue

        existing = existing_by_id or existing_by_username
        if existing is None:
            db.add(FollowedTrader(
                username=username,
                platform_identifier=profile_id,
                profile_url=profile_url,
                tracking_enabled=True,
            ))
            added += 1
            continue

        changed = False
        if existing.username != username:
            existing.username = username
            existing.last_synced_at = None
            existing.sync_error = None
            changed = True
        if profile_id and existing.platform_identifier != profile_id:
            existing.platform_identifier = profile_id
            changed = True
        if existing.profile_url != profile_url:
            existing.profile_url = profile_url
            changed = True
        updated += int(changed)

    db.commit()
    return {"added": added, "updated": updated, "conflicts": conflicts}


def list_followed_users(
    db: Session,
    limit: int = 20,
    offset: int = 0,
    sort_by: str = "date_added",
    search: str | None = None,
) -> list[dict]:
    rows = _followed_user_query(db, search).all()
    performance = trader_performance_stats(db, [row.id for row in rows])
    for row in rows:
        row.performance = performance[row.id]
    sort_values = {
        "score": lambda row: row.performance["performance_score"],
        "success_10x": lambda row: row.performance["success_rate_10x"],
        "median_growth": lambda row: row.performance["median_growth"],
        "total_picks": lambda row: row.performance["total_picks"],
    }
    if sort_by in sort_values:
        rows.sort(key=lambda row: (
            sort_values[sort_by](row) is not None,
            sort_values[sort_by](row) or 0,
            row.username.casefold(),
        ), reverse=True)
    else:
        rows.sort(key=lambda row: row.date_added, reverse=True)
    rows = rows[offset:offset + limit]
    return [
        {
            "id": row.id,
            "username": row.username,
            "platform_identifier": row.platform_identifier,
            "profile_url": row.profile_url or f"https://fomo.family/profile/{quote(row.username, safe='')}",
            "tracking_enabled": row.tracking_enabled,
            "last_synced_at": row.last_synced_at.isoformat() if row.last_synced_at else None,
            "sync_error": row.sync_error,
            **row.performance,
        }
        for row in rows
    ]


def followed_user_count(db: Session, search: str | None = None) -> int:
    return _followed_user_query(db, search).count()


def _followed_user_query(db: Session, search: str | None):
    query = db.query(FollowedTrader)
    term = (search or "").strip().casefold()
    if term:
        query = query.filter(or_(
            func.lower(FollowedTrader.username).contains(term, autoescape=True),
            func.lower(func.coalesce(FollowedTrader.platform_identifier, "")).contains(term, autoescape=True),
        ))
    return query


def delete_followed_user(db: Session, trader_id: int) -> bool:
    return delete_followed_users(db, [trader_id]) > 0


def delete_followed_users(db: Session, trader_ids: list[int]) -> int:
    unique_ids = set(trader_ids)
    if not unique_ids:
        return 0
    existing_ids = {
        trader_id for (trader_id,) in db.query(FollowedTrader.id).filter(FollowedTrader.id.in_(unique_ids)).all()
    }
    if not existing_ids:
        return 0
    db.query(TraderTransactionEvent).filter(TraderTransactionEvent.followed_trader_id.in_(existing_ids)).delete(synchronize_session=False)
    db.query(TraderTokenPick).filter(TraderTokenPick.followed_trader_id.in_(existing_ids)).delete(synchronize_session=False)
    db.query(FollowedWalletAssociation).filter(FollowedWalletAssociation.followed_trader_id.in_(existing_ids)).delete(synchronize_session=False)
    deleted = db.query(FollowedTrader).filter(FollowedTrader.id.in_(existing_ids)).delete(synchronize_session=False)
    db.commit()
    return deleted


def list_followed_buy_activity(db: Session, limit: int = 50) -> list[dict]:
    rows = (
        db.query(TraderTransactionEvent, FollowedTrader.username, Token, Chain.chain_name)
        .join(FollowedTrader, TraderTransactionEvent.followed_trader_id == FollowedTrader.id)
        .join(Token, TraderTransactionEvent.token_id == Token.id)
        .join(Chain, Token.chain_id == Chain.id)
        .filter(
            TraderTransactionEvent.source == "fomo_family",
            TraderTransactionEvent.event_type == "buy",
        )
        .order_by(TraderTransactionEvent.event_time.desc(), TraderTransactionEvent.id.desc())
        .limit(limit)
        .all()
    )
    token_data = token_display_data(db, [token.id for _, _, token, _ in rows])
    return [
        {
            "id": event.id,
            "swap_id": event.dedup_key.removeprefix("fomo:") if event.dedup_key else None,
            "username": username,
            "token_id": token.id,
            "contract_address": token.contract_address,
            "ticker": token.ticker,
            "name": token.name,
            "chain_name": chain_name,
            "token_amount": event.token_amount,
            "estimated_usd": event.estimated_usd,
            "price": event.price_at_event,
            "event_time": event.event_time.isoformat() if event.event_time else None,
            **token_data.get(token.id, {}),
        }
        for event, username, token, chain_name in rows
    ]
