from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.features.discovery.service import list_discoveries

router = APIRouter(prefix="/discoveries", tags=["discoveries"])


@router.get("/")
def get_discoveries(db: Session = Depends(get_db), limit: int = Query(default=20, ge=1, le=200)) -> list[dict]:
    return list_discoveries(db, limit=limit)
