from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.features.risk_analysis.service import count_risk_findings, delete_risk_findings, list_risk_findings

router = APIRouter(prefix="/risk-analysis", tags=["risk_analysis"])


@router.get("/")
def get_risk_findings(
    db: Session = Depends(get_db),
    limit: int = Query(default=100, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    search: str | None = Query(default=None, max_length=200),
) -> list[dict]:
    return list_risk_findings(db, limit=limit, offset=offset, search=search)


@router.get("/count")
def get_risk_finding_count(
    db: Session = Depends(get_db),
    search: str | None = Query(default=None, max_length=200),
) -> dict:
    return {"count": count_risk_findings(db, search=search)}


@router.delete("/bulk-delete")
def remove_risk_findings(finding_ids: list[int] = Body(embed=True), db: Session = Depends(get_db)) -> dict:
    if not finding_ids:
        raise HTTPException(status_code=422, detail="At least one risk finding ID is required")
    return {"deleted": delete_risk_findings(db, finding_ids)}
