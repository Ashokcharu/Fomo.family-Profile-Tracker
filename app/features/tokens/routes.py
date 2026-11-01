from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.features.tokens.service import active_token_count, create_or_get_token, delete_token, delete_tokens, list_recent_bought_tokens, list_token_buyers, list_tokens
from app.models import Token

router = APIRouter(prefix="/tokens", tags=["tokens"])


@router.get("/")
def get_tokens(
    db: Session = Depends(get_db),
    limit: int = Query(default=1000, ge=1, le=2000),
    offset: int = Query(default=0, ge=0),
    search: str | None = Query(default=None, max_length=200),
) -> list[dict]:
    return list_tokens(db, limit=limit, offset=offset, search=search)


@router.get("/count")
def get_token_count(
    db: Session = Depends(get_db),
    search: str | None = Query(default=None, max_length=200),
) -> dict:
    return {"count": active_token_count(db, search=search)}


@router.get("/recent-bought")
def get_recent_bought_tokens(
    db: Session = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
) -> list[dict]:
    return list_recent_bought_tokens(
        db,
        limit=limit,
        offset=offset,
        discovery_threshold_usd=get_settings().discovery_threshold_usd,
    )


@router.get("/{token_id}/buyers")
def get_token_buyers(token_id: int, db: Session = Depends(get_db)) -> list[dict]:
    if db.query(Token.id).filter(Token.id == token_id).first() is None:
        raise HTTPException(status_code=404, detail="Token not found")
    return list_token_buyers(db, token_id)


@router.post("/")
def add_token(chain_name: str, address: str, db: Session = Depends(get_db)) -> dict:
    token = create_or_get_token(db, chain_name, address)
    return {"id": token.id, "chain_id": token.chain_id, "normalized_address": token.normalized_address}


@router.delete("/bulk-delete")
def remove_tokens(token_ids: list[int] = Body(embed=True), db: Session = Depends(get_db)) -> dict:
    if not token_ids:
        raise HTTPException(status_code=422, detail="At least one token ID is required")
    return {"deleted": delete_tokens(db, token_ids)}


@router.delete("/{token_id}", status_code=204)
def remove_token(token_id: int, db: Session = Depends(get_db)) -> Response:
    if not delete_token(db, token_id):
        raise HTTPException(status_code=404, detail="Token not found")
    return Response(status_code=204)
