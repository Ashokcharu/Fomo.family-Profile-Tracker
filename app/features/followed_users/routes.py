from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.features.followed_users.service import add_followed_user, delete_followed_user, delete_followed_users, followed_user_count, list_followed_buy_activity, list_followed_users, normalize_followed_username
from app.models import FollowedTrader

router = APIRouter(prefix="/followed-traders", tags=["followed_traders"])


@router.get("/")
def get_followed_users(
    db: Session = Depends(get_db),
    limit: int = Query(default=100, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    sort_by: Literal["date_added", "score", "success_10x", "median_growth", "total_picks"] = "date_added",
    search: str | None = Query(default=None, max_length=128),
) -> list[dict]:
    return list_followed_users(db, limit=limit, offset=offset, sort_by=sort_by, search=search)


@router.get("/count")
def get_followed_user_count(search: str | None = Query(default=None, max_length=128), db: Session = Depends(get_db)) -> dict:
    return {"count": followed_user_count(db, search)}


@router.get("/activity")
def get_followed_buy_activity(db: Session = Depends(get_db), limit: int = Query(default=50, ge=1, le=200)) -> list[dict]:
    return list_followed_buy_activity(db, limit=limit)


@router.post("/")
def add_user(username: str, request: Request, db: Session = Depends(get_db)) -> dict:
    try:
        normalized = normalize_followed_username(username)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    existing = db.query(FollowedTrader.id).filter(
        func.lower(FollowedTrader.username) == normalized.lower()
    ).first()
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"Duplicate: {normalized} is already followed.")
    user = add_followed_user(db, normalized)
    monitor = getattr(request.app.state, "fomo_activity_monitor", None)
    if monitor is not None:
        monitor.request_sync()
    return {"id": user.id, "username": user.username, "tracking_enabled": user.tracking_enabled}


@router.delete("/bulk-delete")
def remove_users(trader_ids: list[int] = Body(embed=True), db: Session = Depends(get_db)) -> dict:
    if not trader_ids:
        raise HTTPException(status_code=422, detail="At least one followed trader ID is required")
    return {"deleted": delete_followed_users(db, trader_ids)}


@router.delete("/{trader_id}", status_code=204)
def remove_user(trader_id: int, db: Session = Depends(get_db)) -> Response:
    if not delete_followed_user(db, trader_id):
        raise HTTPException(status_code=404, detail="Followed trader not found")
    return Response(status_code=204)
