from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.features.ai_discovery.service import AIDataService

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
