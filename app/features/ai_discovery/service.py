from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import SessionLocal
from app.features.ai_discovery.candidate_features import build_candidate_features
from app.features.tokens.service import list_all_active_tokens
from app.integrations.ollama.client import OllamaClient
from app.integrations.ollama.prompts import build_discovery_prompt
from app.models import AIIntelligenceRecord, BackgroundJob, Favorite

MAX_AI_CANDIDATES = 30
MAX_MODEL_CANDIDATES = 1
MAX_STORED_RUNS_TO_SCAN = 100


class AIDataService:
    def __init__(self, settings: Settings | None = None, ollama_client: OllamaClient | None = None) -> None:
        self.settings = settings or get_settings()
        self.ollama = ollama_client or OllamaClient(self.settings.ollama_base_url, self.settings.ollama_model)

    def get_status(self, db: Session) -> dict[str, Any]:
        enabled = self.settings.ai_discovery_enabled
        model_available = self.ollama.is_available() if enabled else False
        latest_run = self._latest_run(db)
        latest_job = (
            db.query(BackgroundJob)
            .filter(BackgroundJob.job_type == "ai_discovery")
            .order_by(BackgroundJob.id.desc())
            .first()
        )
        if not enabled:
            status = "disabled"
            note = "Enable AI_DISCOVERY_ENABLED to analyze tracked tokens."
        elif latest_job and latest_job.status in {"queued", "running", "model_pending"}:
            status = {
                "queued": "scan_pending",
                "running": "scanning",
                "model_pending": "model_pending",
            }[latest_job.status]
            note = (latest_job.payload or {}).get("note", "AI discovery is running in the background.")
        elif latest_job and latest_job.status == "failed":
            status = "failed"
            note = latest_job.error_message or "AI discovery failed."
        elif latest_job and latest_job.status == "completed" and (latest_job.payload or {}).get("result_status") == "no_tracked_tokens":
            status = "no_tracked_tokens"
            note = "No active tokens are currently listed in The Tokens tab."
        elif latest_run:
            status = latest_run["status"]
            note = latest_run["note"]
        elif model_available:
            status = "ready"
            note = "Ready to analyze active tokens from The Tokens tab only."
        else:
            status = "fallback_ready"
            note = "Ollama is unavailable; the run button will use transparent signal scoring instead."

        candidates = latest_run["candidates"] if latest_run else []
        if candidates:
            candidate_ids = [candidate["token_id"] for candidate in candidates]
            favorited_ids = {
                token_id for (token_id,) in db.query(Favorite.token_id)
                .filter(Favorite.token_id.in_(candidate_ids))
                .all()
            }
            candidates = [
                {**candidate, "is_favorite": candidate["token_id"] in favorited_ids}
                for candidate in candidates
            ]
        return {
            "enabled": enabled,
            "status": status,
            "model": self.settings.ollama_model,
            "model_available": model_available,
            "note": note,
            "tokens_scanned": latest_run["tokens_scanned"] if latest_run else 0,
            "candidate_count": len(latest_run["candidates"]) if latest_run else 0,
            "last_run_at": latest_run["last_run_at"] if latest_run else None,
            "candidates": candidates,
            "job_id": latest_job.id if latest_job else None,
        }

    def queue_discovery(self, db: Session) -> dict[str, Any]:
        if not self.settings.ai_discovery_enabled:
            return self.get_status(db)
        job = BackgroundJob(
            job_type="ai_discovery",
            status="queued",
            payload={
                "model": self.settings.ollama_model,
                "note": "Scanning active tokens from The Tokens in the background.",
            },
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        status = self.get_status(db)
        status["job_id"] = job.id
        return status

    def run_queued_discovery(self, job_id: int, session_factory=SessionLocal) -> None:
        with session_factory() as db:
            job = db.get(BackgroundJob, job_id)
            if job is None:
                return
            job.status = "running"
            job.payload = {**(job.payload or {}), "note": "Scanning active tokens from The Tokens."}
            job.started_at = datetime.now(timezone.utc)
            db.commit()
            try:
                result = self.run_discovery(db, defer_model=True)
                job = db.get(BackgroundJob, job_id)
                if result["status"] == "model_pending":
                    job.status = "model_pending"
                    job.payload = {**(job.payload or {}), "note": result["note"]}
                    db.commit()
                    self.complete_model_assessment(result["candidates"][0]["run_id"], session_factory)
                    job = db.get(BackgroundJob, job_id)
                job.status = "completed"
                job.payload = {
                    **(job.payload or {}),
                    "result_status": "model_assisted" if result["status"] == "model_pending" else result["status"],
                    "note": result["note"],
                }
                job.finished_at = datetime.now(timezone.utc)
                db.commit()
            except Exception as exc:
                db.rollback()
                job = db.get(BackgroundJob, job_id)
                if job is not None:
                    job.status = "failed"
                    job.error_message = str(exc)[:255]
                    job.finished_at = datetime.now(timezone.utc)
                    db.commit()

    def run_discovery(self, db: Session, defer_model: bool = False) -> dict[str, Any]:
        if not self.settings.ai_discovery_enabled:
            return self.get_status(db)

        tracked_tokens = list_all_active_tokens(db)
        candidates = [build_candidate_features(token, self.settings) for token in tracked_tokens]
        candidates.sort(key=lambda row: (row["heuristic_score"], row["token_id"]), reverse=True)
        candidates = candidates[:MAX_AI_CANDIDATES]
        run_at = datetime.now(timezone.utc).isoformat()
        run_id = uuid4().hex
        model_available = self.ollama.is_available()
        model_pending = defer_model and model_available and bool(candidates)
        assessments = (
            self._get_model_assessments(candidates[:MAX_MODEL_CANDIDATES])
            if model_available and candidates and not defer_model else {}
        )
        model_used = bool(assessments)

        stored_candidates = []
        for candidate in candidates:
            assessment = assessments.get(candidate["token_id"], {})
            model_score = assessment.get("score")
            score = candidate["heuristic_score"]
            if model_score is not None:
                score = score * 0.7 + model_score * 0.3
            score = round(max(0.0, min(100.0, score)), 1)
            if score >= 70:
                recommendation = "High potential"
            elif score >= 50:
                recommendation = "Review"
            else:
                recommendation = "Low signal"
            summary = assessment.get("summary") or _fallback_summary(candidate)
            saved = {
                **candidate,
                "score": score,
                "model_score": model_score,
                "recommendation": recommendation,
                "summary": summary,
                "model": self.settings.ollama_model if model_score is not None else None,
                "model_pending": model_pending,
                "run_id": run_id,
                "last_run_at": run_at,
                "tokens_scanned": len(tracked_tokens),
            }
            stored_candidates.append(saved)
        stored_candidates.sort(
            key=lambda row: (row["score"], row["heuristic_score"], row["token_id"]),
            reverse=True,
        )
        for rank, candidate in enumerate(stored_candidates, start=1):
            candidate["rank"] = rank
            db.add(AIIntelligenceRecord(
                token_id=candidate["token_id"],
                record_type="ai_discovery_candidate",
                source="ai_discovery",
                manually_entered=False,
                data=candidate,
            ))
        db.commit()

        if not tracked_tokens:
            status = "no_tracked_tokens"
            note = "No active tokens are currently listed in The Tokens tab."
        elif model_pending:
            status = "model_pending"
            note = f"Ranked {len(tracked_tokens)} active tracked tokens using observable signals. {self.settings.ollama_model} is assessing the top candidate in the background."
        elif model_used:
            status = "model_assisted"
            note = f"Ranked {len(tracked_tokens)} active tracked tokens; showing the top {len(stored_candidates)}. Ollama assessed the top {len(assessments)} signal candidates."
        else:
            status = "heuristic_fallback"
            note = f"Assessed {len(tracked_tokens)} active tracked tokens using observable signals; Ollama was unavailable or returned no valid assessments."
        response = {
            "enabled": True,
            "status": status,
            "model": self.settings.ollama_model,
            "model_available": model_available,
            "note": note,
            "tokens_scanned": len(tracked_tokens),
            "candidate_count": len(stored_candidates),
            "last_run_at": run_at,
            "candidates": stored_candidates,
        }
        return response

    def complete_model_assessment(self, run_id: str, session_factory=SessionLocal) -> None:
        with session_factory() as db:
            records = (
                db.query(AIIntelligenceRecord)
                .filter(
                    AIIntelligenceRecord.record_type == "ai_discovery_candidate",
                    AIIntelligenceRecord.source == "ai_discovery",
                )
                .order_by(AIIntelligenceRecord.id.desc())
                .limit(MAX_STORED_RUNS_TO_SCAN)
                .all()
            )
            run_records = [
                record for record in records
                if isinstance(record.data, dict) and record.data.get("run_id") == run_id
            ]
            if not run_records:
                return
            run_records.sort(key=lambda record: record.data.get("rank", 0))
            assessments = self._get_model_assessments([run_records[0].data])
            assessment = assessments.get(run_records[0].token_id)
            for record in run_records:
                data = dict(record.data or {})
                data["model_pending"] = False
                if record is run_records[0] and assessment:
                    model_score = assessment["score"]
                    data["model_score"] = model_score
                    data["score"] = round(data["heuristic_score"] * 0.7 + model_score * 0.3, 1)
                    data["summary"] = assessment.get("summary") or data["summary"]
                    data["model"] = self.settings.ollama_model
                    data["recommendation"] = _recommendation(data["score"])
                record.data = data
            run_records.sort(
                key=lambda record: (
                    record.data["score"],
                    record.data["heuristic_score"],
                    record.token_id,
                ),
                reverse=True,
            )
            for rank, record in enumerate(run_records, start=1):
                data = dict(record.data)
                data["rank"] = rank
                record.data = data
            db.commit()

    def _get_model_assessments(self, candidates: list[dict]) -> dict[int, dict]:
        model_candidates = [
            {
                key: candidate.get(key)
                for key in (
                    "token_id",
                    "name",
                    "ticker",
                    "chain_name",
                    "market_cap",
                    "liquidity",
                    "buyer_count",
                    "fomo_holder_count",
                    "growth_multiple",
                    "heuristic_score",
                    "score_breakdown",
                    "strengths",
                    "risks",
                )
            }
            for candidate in candidates[:MAX_MODEL_CANDIDATES]
        ]
        result = self.ollama.generate(build_discovery_prompt(model_candidates))
        if result.get("status") != "ok":
            return {}
        try:
            payload = json.loads(result.get("response", ""))
        except (TypeError, ValueError):
            return {}
        allowed_ids = {candidate["token_id"] for candidate in model_candidates}
        assessments = {}
        for item in payload.get("assessments", []) if isinstance(payload, dict) else []:
            if not isinstance(item, dict):
                continue
            try:
                token_id = int(item.get("token_id"))
                score = float(item.get("score"))
            except (TypeError, ValueError):
                continue
            if token_id not in allowed_ids or not 0 <= score <= 100:
                continue
            summary = item.get("summary")
            if not isinstance(summary, str):
                summary = None
            assessments[token_id] = {"score": score, "summary": summary[:240] if summary else None}
        return assessments

    def _latest_run(self, db: Session) -> dict[str, Any] | None:
        records = (
            db.query(AIIntelligenceRecord)
            .filter(
                AIIntelligenceRecord.record_type == "ai_discovery_candidate",
                AIIntelligenceRecord.source == "ai_discovery",
            )
            .order_by(AIIntelligenceRecord.created_at.desc(), AIIntelligenceRecord.id.desc())
            .limit(MAX_STORED_RUNS_TO_SCAN)
            .all()
        )
        if not records:
            return None
        newest_data = records[0].data if isinstance(records[0].data, dict) else {}
        run_id = newest_data.get("run_id")
        if not run_id:
            return None
        run_records = [
            record for record in records
            if isinstance(record.data, dict) and record.data.get("run_id") == run_id
        ]
        candidates = [record.data for record in run_records]
        candidates.sort(key=lambda row: row.get("rank", 0))
        scanned = int(candidates[0].get("tokens_scanned", 0)) if candidates else 0
        last_run_at = candidates[0].get("last_run_at") if candidates else None
        model_used = any(candidate.get("model_score") is not None for candidate in candidates)
        model_pending = any(candidate.get("model_pending") for candidate in candidates)
        if model_used:
            status = "model_assisted"
            note = f"Ranked {scanned} active tracked tokens; showing the top {len(candidates)}. Ollama assessed the top signal candidate."
        elif model_pending:
            status = "model_pending"
            note = f"Ranked {scanned} active tracked tokens using observable signals. {self.settings.ollama_model} is assessing the top candidate in the background."
        else:
            status = "heuristic_fallback"
            note = f"Showing the saved top {len(candidates)} from {scanned} active tracked tokens using observable signal scoring."
        return {"candidates": candidates, "tokens_scanned": scanned, "last_run_at": last_run_at, "status": status, "note": note}


def _fallback_summary(candidate: dict) -> str:
    strengths = "; ".join(candidate["strengths"][:2])
    risks = "; ".join(candidate["risks"][:2])
    parts = [part for part in (strengths, f"Risks: {risks}" if risks else "No tracked risk flags") if part]
    return ". ".join(parts)[:240] or "Insufficient tracked market data for a detailed assessment."


def _recommendation(score: float) -> str:
    if score >= 70:
        return "High potential"
    if score >= 50:
        return "Review"
    return "Low signal"