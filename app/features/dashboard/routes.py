from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.features.dashboard.service import get_overview

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/overview")
def overview(db: Session = Depends(get_db)) -> dict:
    return get_overview(db)
