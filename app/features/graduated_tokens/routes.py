from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.features.graduated_tokens.service import count_graduated_tokens, list_graduated_tokens

router = APIRouter(prefix="/graduated-tokens", tags=["graduated_tokens"])


@router.get("/")
def get_graduated_tokens(
    db: Session = Depends(get_db),
    limit: int = Query(default=100, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    search: str | None = Query(default=None, max_length=200),
) -> list[dict]:
    return list_graduated_tokens(db, limit=limit, offset=offset, search=search)


@router.get("/count")
def get_graduated_token_count(
    db: Session = Depends(get_db),
    search: str | None = Query(default=None, max_length=200),
) -> dict:
    return {"count": count_graduated_tokens(db, search=search)}
