from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.features.favorites.service import count_favorites, delete_favorite, delete_favorites, favorite_token, list_favorites
from app.models import MarketObservation, Token

router = APIRouter(prefix="/favorites", tags=["favorites"])


@router.get("/")
def get_favorites(
    db: Session = Depends(get_db),
    limit: int = Query(default=100, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    search: str | None = Query(default=None, max_length=200),
) -> list[dict]:
    return list_favorites(db, limit=limit, offset=offset, search=search)


@router.get("/count")
def get_favorite_count(
    db: Session = Depends(get_db),
    search: str | None = Query(default=None, max_length=200),
) -> dict:
    return {"count": count_favorites(db, search=search)}


@router.post("/{token_id}")
def add_favorite(token_id: int, db: Session = Depends(get_db)) -> dict:
    token = db.query(Token).filter(Token.id == token_id).first()
    if token is None:
        raise HTTPException(status_code=404, detail="Token not found")
    observation = (
        db.query(MarketObservation)
        .filter(MarketObservation.token_id == token_id, MarketObservation.is_valid.is_(True))
        .order_by(MarketObservation.observed_at.desc(), MarketObservation.id.desc())
        .first()
    )
    favorite = favorite_token(
        db,
        token,
        source="tracked_tokens",
        market_cap=observation.market_cap if observation else None,
        price=observation.price if observation else None,
        liquidity=observation.liquidity if observation else None,
    )
    return {"id": favorite.id, "token_id": favorite.token_id}


@router.delete("/bulk-delete")
def remove_favorites(favorite_ids: list[int] = Body(embed=True), db: Session = Depends(get_db)) -> dict:
    if not favorite_ids:
        raise HTTPException(status_code=422, detail="At least one favorite ID is required")
    return {"deleted": delete_favorites(db, favorite_ids)}


@router.delete("/{favorite_id}")
def remove_favorite(favorite_id: int, db: Session = Depends(get_db)) -> dict:
    if not delete_favorite(db, favorite_id):
        raise HTTPException(status_code=404, detail="Favorite not found")
    return {"deleted": True}
