from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.features.tokens.service import filter_token_records, token_display_data
from app.models import Favorite, Token


def list_favorites(db: Session, limit: int = 20, offset: int = 0, search: str | None = None) -> list[dict]:
    query = filter_token_records(db.query(Favorite), Favorite.token_id, search)
    rows = query.order_by(Favorite.favorited_at.desc()).offset(offset).limit(limit).all()
    token_data = token_display_data(db, [row.token_id for row in rows])
    return [
        {
            "id": row.id,
            "token_id": row.token_id,
            "ticker": row.token.ticker,
            "name": row.token.name,
            "contract_address": row.token.contract_address,
            "chain_name": row.token.chain.chain_name,
            "favorited_at": row.favorited_at.isoformat(),
            "market_cap_at_favorite": row.market_cap_at_favorite,
            "price_at_favorite": row.price_at_favorite,
            "liquidity_at_favorite": row.liquidity_at_favorite,
            "source": row.source,
            **token_data.get(row.token_id, {}),
        }
        for row in rows
    ]


def count_favorites(db: Session, search: str | None = None) -> int:
    query = filter_token_records(db.query(func.count(Favorite.id)), Favorite.token_id, search)
    return query.scalar() or 0


def favorite_token(db: Session, token: Token, source: str | None = None, market_cap: float | None = None, price: float | None = None, liquidity: float | None = None) -> Favorite:
    existing = db.query(Favorite).filter(Favorite.token_id == token.id).first()
    if existing is not None:
        return existing
    favorite = Favorite(
        token_id=token.id,
        source=source,
        market_cap_at_favorite=market_cap,
        price_at_favorite=price,
        liquidity_at_favorite=liquidity,
    )
    db.add(favorite)
    db.commit()
    db.refresh(favorite)
    return favorite


def delete_favorite(db: Session, favorite_id: int) -> bool:
    return delete_favorites(db, [favorite_id]) > 0


def delete_favorites(db: Session, favorite_ids: list[int]) -> int:
    unique_ids = set(favorite_ids)
    if not unique_ids:
        return 0
    deleted = db.query(Favorite).filter(Favorite.id.in_(unique_ids)).delete(synchronize_session=False)
    db.commit()
    return deleted
