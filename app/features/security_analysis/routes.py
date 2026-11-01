from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.features.security_analysis.service import SecurityAnalysisService
from app.models import SecurityAnalysis

router = APIRouter(prefix="/security", tags=["security"])
service = SecurityAnalysisService()


@router.get("/")
def list_security_analyses(
    db: Session = Depends(get_db),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> list[dict]:
    return service.list_recent_analyses(db, limit=limit, offset=offset)


@router.get("/count")
def security_analysis_count(db: Session = Depends(get_db)) -> dict:
    return {"count": db.query(SecurityAnalysis).count()}


@router.get("/{token_id}")
def get_security_analysis(token_id: int, db: Session = Depends(get_db)) -> dict:
    result = service.get_analysis(db, token_id)
    if result is None:
        try:
            result = service.analyze_token(db, token_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="Security analysis not found") from exc
    return result


@router.post("/{token_id}/analyze")
def analyze_security(token_id: int, db: Session = Depends(get_db)) -> dict:
    try:
        return service.analyze_token(db, token_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{token_id}/creator")
def get_creator_activity(token_id: int, db: Session = Depends(get_db)) -> dict:
    result = service.get_analysis(db, token_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Security analysis not found")
    return {
        "token_id": token_id,
        "creator_activity": result.get("creator_activity", {}),
        "creator_wallet_cluster": result.get("creator_wallet_cluster", {}),
    }


@router.get("/{token_id}/summary")
def get_security_summary(token_id: int, db: Session = Depends(get_db)) -> dict:
    result = service.get_analysis(db, token_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Security analysis not found")
    return {
        "token_id": token_id,
        "risk_score": result.get("risk_score"),
        "risk_level": result.get("risk_level"),
        "analysis_status": result.get("analysis_status"),
        "confidence": result.get("confidence"),
        "implementation_phase": result.get("analysis_metadata", {}).get("phase", "phase_1"),
        "provider_status": result.get("analysis_metadata", {}).get("provider_status", {}),
    }


@router.get("/{token_id}/trend")
def get_security_trend(token_id: int, db: Session = Depends(get_db)) -> dict:
    return service.get_historical_trend(db, token_id)


@router.get("/coverage")
def get_security_coverage(db: Session = Depends(get_db)) -> dict:
    return service.get_provider_coverage_summary(db)
