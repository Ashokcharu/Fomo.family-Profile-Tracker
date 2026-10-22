from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.features.market_data.service import list_market_history

router = APIRouter(prefix="/market-data", tags=["market_data"])


@router.get("/history")
def market_history(db: Session = Depends(get_db), limit: int = Query(default=20, ge=1, le=200)) -> list[dict]:
    return list_market_history(db, limit=limit)
