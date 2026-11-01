from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.features.dismissed_tokens.service import count_dismissed_tokens, delete_dismissed_tokens, dismiss_tokens, list_dismissed_tokens

router = APIRouter(prefix="/dismissed-tokens", tags=["dismissed_tokens"])


@router.get("/")
def get_dismissed_tokens(
    db: Session = Depends(get_db),
    limit: int = Query(default=100, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    search: str | None = Query(default=None, max_length=200),
) -> list[dict]:
    return list_dismissed_tokens(db, limit=limit, offset=offset, search=search)


@router.get("/count")
def get_dismissed_token_count(
    db: Session = Depends(get_db),
    search: str | None = Query(default=None, max_length=200),
) -> dict:
    return {"count": count_dismissed_tokens(db, search=search)}


@router.post("/bulk-dismiss")
def dismiss_selected_tokens(token_ids: list[int] = Body(embed=True), db: Session = Depends(get_db)) -> dict:
    if not token_ids:
        raise HTTPException(status_code=422, detail="At least one token ID is required")
    return dismiss_tokens(db, token_ids, reason="Dismissed from Risk signals")


@router.delete("/bulk-delete")
def remove_dismissed_tokens(dismissed_ids: list[int] = Body(embed=True), db: Session = Depends(get_db)) -> dict:
    if not dismissed_ids:
        raise HTTPException(status_code=422, detail="At least one dismissed record ID is required")
    return {"deleted": delete_dismissed_tokens(db, dismissed_ids)}
