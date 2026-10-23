from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.features.ai_discovery.service import AIDataService
from app.features.ai_discovery.schemas import DiscoveryFeedback

router = APIRouter(prefix="/ai-discovery", tags=["ai_discovery"])
service = AIDataService()


@router.get("/")
def get_ai_status(db: Session = Depends(get_db)) -> dict:
    return service.get_status(db)


@router.post("/run")
def run_ai_discovery(background_tasks: BackgroundTasks, db: Session = Depends(get_db)) -> dict:
    result = service.queue_discovery(db)
    if result.get("job_id") is not None and result.get("status") == "scan_pending":
        background_tasks.add_task(service.run_queued_discovery, result["job_id"])
    return result


@router.post("/feedback/{token_id}")
def rate_ai_candidate(token_id: int, feedback: DiscoveryFeedback, db: Session = Depends(get_db)) -> dict:
    if not service.record_feedback(db, token_id, feedback.rating):
        raise HTTPException(status_code=404, detail="Token not found")
    return {"token_id": token_id, "rating": feedback.rating}


@router.delete("/top-picks/{token_id}")
def remove_ai_top_pick(token_id: int, db: Session = Depends(get_db)) -> dict:
    if not service.remove_top_pick(db, token_id):
        raise HTTPException(status_code=404, detail="Active Top 3 pick not found")
    return {"token_id": token_id, "removed": True}
