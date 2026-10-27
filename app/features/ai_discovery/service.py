from __future__ import annotations

import json
from datetime import datetime, timezone
from time import monotonic
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import SessionLocal
from app.features.ai_discovery.candidate_features import build_candidate_features
from app.features.tokens.service import list_active_token_ids, list_all_active_tokens, token_display_data
from app.integrations.ollama.client import OllamaClient
from app.integrations.ollama.prompts import build_discovery_prompt
from app.models import AIIntelligenceRecord, BackgroundJob, Favorite, MarketObservation, Token

MAX_AI_CANDIDATES = 50
MAX_MODEL_CANDIDATES = 1
MAX_STORED_RUNS_TO_SCAN = 100
MAX_TOP_PICKS = 3
MIN_ACTIVE_CANDIDATE_SCORE = 50.0
FEEDBACK_ADJUSTMENT = 8.0
MODEL_AVAILABILITY_CACHE_SECONDS = 300


class AIDataService:
    def __init__(self, settings: Settings | None = None, ollama_client: OllamaClient | None = None) -> None:
        self.settings = settings or get_settings()
        self.ollama = ollama_client or OllamaClient(
            self.settings.ollama_base_url,
            self.settings.ollama_model,
            self.settings.ollama_num_threads,
        )
        self._model_availability_checked_at = 0.0
        self._model_availability_cache = False

    def _model_available(self) -> bool:
        now = monotonic()
        if now - self._model_availability_checked_at >= MODEL_AVAILABILITY_CACHE_SECONDS:
            self._model_availability_cache = self.ollama.is_available()
            self._model_availability_checked_at = now
        return self._model_availability_cache

    def get_status(self, db: Session) -> dict[str, Any]:
        enabled = self.settings.ai_discovery_enabled
        model_available = self._model_available() if enabled else False
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
            excluded_ids = self._excluded_candidate_ids(db, [candidate["token_id"] for candidate in candidates])
            candidates = [candidate for candidate in candidates if candidate["token_id"] not in excluded_ids]
        if candidates:
            candidate_ids = [candidate["token_id"] for candidate in candidates]
            baselines = self._records_by_token(db, "ai_discovery_baseline", candidate_ids)
            feedback = self._latest_feedback(db, candidate_ids)
            favorited_ids = {
                token_id for (token_id,) in db.query(Favorite.token_id)
                .filter(Favorite.token_id.in_(candidate_ids))
                .all()
            }
            refreshed_candidates = []
            for candidate in candidates:
                baseline = baselines.get(candidate["token_id"])
                baseline_data = baseline.data if baseline and isinstance(baseline.data, dict) else {}
                candidate["ai_rh"] = max(
                    (value for value in (
                        candidate.get("ai_rh"),
                        baseline_data.get("ai_rh"),
                        baseline_data.get("ai_rmc"),
                        candidate.get("market_cap"),
                    ) if value is not None),
                    default=None,
                )
                if baseline and baseline_data.get("ai_rh") is None:
                    baseline.data = {**baseline_data, "ai_rh": candidate["ai_rh"]}
                rating = feedback.get(candidate["token_id"])
                adjustment = FEEDBACK_ADJUSTMENT if rating == "positive" else -FEEDBACK_ADJUSTMENT if rating == "negative" else 0.0
                base_score = candidate["heuristic_score"]
                if candidate.get("model_score") is not None:
                    base_score = base_score * 0.7 + candidate["model_score"] * 0.3
                candidate = {
                    **candidate,
                    "feedback": rating,
                    "feedback_adjustment": adjustment,
                    "score": round(max(
                        0.0,
                        min(100.0, base_score + adjustment + candidate.get("continuation_adjustment", 0)),
                    ), 1),
                    "is_favorite": candidate["token_id"] in favorited_ids,
                    "ai_discovery_since": _datetime_iso_utc(baseline.created_at) if baseline else candidate.get("last_run_at"),
                }
                candidate["recommendation"] = _recommendation(candidate["score"])
                refreshed_candidates.append(candidate)
            candidates = sorted(
                refreshed_candidates,
                key=lambda row: (row["score"], row["heuristic_score"], row["token_id"]),
                reverse=True,
            )
            for rank, candidate in enumerate(candidates, start=1):
                candidate["rank"] = rank
        self._update_top_picks(db, candidates, {})
        db.commit()
        top_picks = self._active_top_picks(db)
        return {
            "enabled": enabled,
            "status": status,
            "model": self.settings.ollama_model,
            "model_available": model_available,
            "note": note,
            "tokens_scanned": latest_run["tokens_scanned"] if latest_run else 0,
            "candidate_count": len(latest_run["candidates"]) if latest_run else 0,
                        "candidate_count": len(candidates),
            "last_run_at": latest_run["last_run_at"] if latest_run else None,
            "candidates": candidates,
            "top_picks": top_picks,
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

    def run_discovery(
        self,
        db: Session,
        defer_model: bool = False,
        use_model: bool = True,
    ) -> dict[str, Any]:
        if not self.settings.ai_discovery_enabled:
            return self.get_status(db)

        tracked_tokens = list_all_active_tokens(db)
        token_features = [build_candidate_features(token, self.settings) for token in tracked_tokens]
        excluded_ids = self._excluded_candidate_ids(db, [candidate["token_id"] for candidate in token_features])
        token_features = [candidate for candidate in token_features if candidate["token_id"] not in excluded_ids]
        feature_by_id = {candidate["token_id"]: candidate for candidate in token_features}
        candidate_ids = [candidate["token_id"] for candidate in token_features]
        baselines = self._records_by_token(db, "ai_discovery_baseline", candidate_ids)
        feedback = self._refresh_automatic_feedback(db, token_features, baselines)
        prior_run = self._latest_run(db)
        prior_candidates = {
            candidate["token_id"]: candidate
            for candidate in (prior_run or {}).get("candidates", [])
        }
        prior_candidate_ids = {
            candidate["token_id"] for candidate in prior_candidates.values()
        }
        for candidate in token_features:
            baseline = baselines.get(candidate["token_id"])
            baseline_data = baseline.data if baseline and isinstance(baseline.data, dict) else {}
            ai_rmc = baseline_data.get("ai_rmc") or candidate.get("market_cap")
            candidate["ai_rmc"] = ai_rmc
            candidate["ai_rh"] = max(
                (value for value in (baseline_data.get("ai_rh"), ai_rmc, candidate.get("market_cap")) if value is not None),
                default=None,
            )
            candidate["ai_growth_multiple"] = (
                round(candidate["market_cap"] / ai_rmc, 3)
                if candidate.get("market_cap") is not None and ai_rmc else None
            )
            candidate["continuation_adjustment"] = _continuation_adjustment(
                candidate["ai_growth_multiple"],
                candidate["token_id"] in prior_candidate_ids,
            )
            rating = feedback.get(candidate["token_id"])
            candidate["feedback"] = rating
            candidate["feedback_adjustment"] = (
                FEEDBACK_ADJUSTMENT if rating == "positive"
                else -FEEDBACK_ADJUSTMENT if rating == "negative"
                else 0.0
            )
            candidate["selection_score"] = max(
                0.0,
                min(
                    100.0,
                    candidate["heuristic_score"]
                    + candidate["continuation_adjustment"]
                    + candidate["feedback_adjustment"],
                ),
            )
        candidates = sorted(
            token_features,
            key=lambda row: (row["selection_score"], row["heuristic_score"], row["token_id"]),
            reverse=True,
        )[:MAX_AI_CANDIDATES]
        active_pick_records = (
            db.query(AIIntelligenceRecord)
            .filter(
                AIIntelligenceRecord.record_type == "ai_discovery_top_pick",
                AIIntelligenceRecord.source == "ai_discovery",
            )
            .all()
        )
        active_pick_ids = {
            record.token_id for record in active_pick_records
            if isinstance(record.data, dict) and record.data.get("active")
        }
        ai_tracking_ids = (
            {candidate["token_id"] for candidate in candidates} | active_pick_ids
        ) & set(baselines)
        if ai_tracking_ids:
            observed_ai_highs = dict(
                db.query(MarketObservation.token_id, func.max(MarketObservation.market_cap))
                .join(
                    AIIntelligenceRecord,
                    and_(
                        MarketObservation.token_id == AIIntelligenceRecord.token_id,
                        AIIntelligenceRecord.record_type == "ai_discovery_baseline",
                        AIIntelligenceRecord.source == "ai_discovery",
                    ),
                )
                .filter(
                    AIIntelligenceRecord.token_id.in_(ai_tracking_ids),
                    MarketObservation.observed_at > AIIntelligenceRecord.created_at,
                    MarketObservation.is_valid.is_(True),
                    MarketObservation.market_cap.is_not(None),
                )
                .group_by(MarketObservation.token_id)
                .all()
            )
            for token_id in ai_tracking_ids:
                baseline = baselines[token_id]
                baseline_data = baseline.data if isinstance(baseline.data, dict) else {}
                candidate = feature_by_id[token_id]
                candidate["ai_rh"] = max(
                    (value for value in (
                        baseline_data.get("ai_rh"),
                        baseline_data.get("ai_rmc"),
                        observed_ai_highs.get(token_id),
                        candidate.get("market_cap"),
                    ) if value is not None),
                    default=None,
                )
                baseline.data = {**baseline_data, "ai_rh": candidate["ai_rh"]}
        for candidate in candidates:
            if candidate["token_id"] not in baselines and candidate.get("market_cap") is not None:
                baseline = AIIntelligenceRecord(
                    token_id=candidate["token_id"],
                    record_type="ai_discovery_baseline",
                    source="ai_discovery",
                    manually_entered=False,
                    data={"ai_rmc": candidate["market_cap"], "ai_rh": candidate["ai_rh"]},
                )
                db.add(baseline)
                baselines[candidate["token_id"]] = baseline
                candidate["ai_rmc"] = candidate["market_cap"]
                candidate["ai_growth_multiple"] = 1.0
        run_at = datetime.now(timezone.utc).isoformat()
        run_id = uuid4().hex
        model_available = self.ollama.is_available() if use_model else False
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
            score += candidate["feedback_adjustment"] + candidate["continuation_adjustment"]
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
        active_candidates = []
        for candidate in stored_candidates:
            if (
                candidate["score"] < MIN_ACTIVE_CANDIDATE_SCORE
                and candidate["token_id"] in prior_candidate_ids
                and not candidate["model_pending"]
            ):
                self._archive_candidate(db, candidate["token_id"], candidate, "failed_low_signal")
            else:
                active_candidates.append(candidate)
        active_candidate_ids = {candidate["token_id"] for candidate in active_candidates}
        for token_id, candidate in prior_candidates.items():
            if token_id in active_candidate_ids or token_id in excluded_ids:
                continue
            reason = "no_longer_eligible" if token_id not in feature_by_id else "replaced_by_better_candidates"
            self._archive_candidate(db, token_id, candidate, reason)

        stored_candidates = active_candidates
        for candidate in stored_candidates:
            db.add(AIIntelligenceRecord(
                token_id=candidate["token_id"],
                record_type="ai_discovery_candidate",
                source="ai_discovery",
                manually_entered=False,
                data=candidate,
            ))
        self._update_top_picks(db, stored_candidates, feature_by_id)
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
            "top_picks": self._active_top_picks(db),
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
                    data["score"] = round(
                        data["heuristic_score"] * 0.7
                        + model_score * 0.3
                        + data.get("feedback_adjustment", 0)
                        + data.get("continuation_adjustment", 0),
                        1,
                    )
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
                if data["score"] < MIN_ACTIVE_CANDIDATE_SCORE:
                    self._archive_candidate(db, record.token_id, data, "failed_low_signal")
            db.commit()

    def record_feedback(self, db: Session, token_id: int, rating: str) -> bool:
        if rating not in {"positive", "negative"}:
            raise ValueError("rating must be positive or negative")
        if db.get(Token, token_id) is None:
            return False
        db.add(AIIntelligenceRecord(
            token_id=token_id,
            record_type="ai_discovery_feedback",
            source="user",
            manually_entered=True,
            data={"rating": rating, "recorded_at": datetime.now(timezone.utc).isoformat()},
        ))
        db.commit()
        return True

    def remove_top_pick(self, db: Session, token_id: int) -> bool:
        record = (
            db.query(AIIntelligenceRecord)
            .filter(
                AIIntelligenceRecord.token_id == token_id,
                AIIntelligenceRecord.record_type == "ai_discovery_top_pick",
                AIIntelligenceRecord.source == "ai_discovery",
            )
            .order_by(AIIntelligenceRecord.id.desc())
            .first()
        )
        if record is None or not isinstance(record.data, dict) or not record.data.get("active"):
            return False
        record.data = {**record.data, "active": False, "removed_at": datetime.now(timezone.utc).isoformat()}
        latest_run = self._latest_run(db)
        self._update_top_picks(db, latest_run["candidates"] if latest_run else [], {})
        db.commit()
        return True

    def remove_candidate(self, db: Session, token_id: int) -> bool:
        if db.get(Token, token_id) is None:
            return False
        candidate_record = (
            db.query(AIIntelligenceRecord)
            .filter(
                AIIntelligenceRecord.token_id == token_id,
                AIIntelligenceRecord.record_type == "ai_discovery_candidate",
                AIIntelligenceRecord.source == "ai_discovery",
            )
            .order_by(AIIntelligenceRecord.id.desc())
            .first()
        )
        candidate_data = candidate_record.data if candidate_record and isinstance(candidate_record.data, dict) else {}
        self._archive_candidate(db, token_id, candidate_data, "manually_removed")
        db.commit()
        return True

    def _archive_candidate(
        self,
        db: Session,
        token_id: int,
        candidate: dict[str, Any],
        reason: str,
    ) -> None:
        exclusion = (
            db.query(AIIntelligenceRecord)
            .filter(
                AIIntelligenceRecord.token_id == token_id,
                AIIntelligenceRecord.record_type == "ai_discovery_exclusion",
                AIIntelligenceRecord.source == "ai_discovery",
            )
            .first()
        )
        if exclusion is not None:
            return
        db.add(AIIntelligenceRecord(
            token_id=token_id,
            record_type="ai_discovery_exclusion",
            source="ai_discovery",
            manually_entered=reason == "manually_removed",
            data={
                "excluded_at": datetime.now(timezone.utc).isoformat(),
                "reason": reason,
                "candidate": candidate,
            },
        ))

    def list_analyzed_candidates(self, db: Session) -> list[dict[str, Any]]:
        exclusions = (
            db.query(AIIntelligenceRecord)
            .filter(
                AIIntelligenceRecord.record_type == "ai_discovery_exclusion",
                AIIntelligenceRecord.source == "ai_discovery",
            )
            .order_by(AIIntelligenceRecord.id.desc())
            .all()
        )
        latest_exclusions = {}
        for exclusion in exclusions:
            if exclusion.token_id is not None and exclusion.token_id not in latest_exclusions:
                latest_exclusions[exclusion.token_id] = exclusion
        if not latest_exclusions:
            return []

        token_ids = list(latest_exclusions)
        display_data_by_token = token_display_data(db, token_ids)
        baselines = self._records_by_token(db, "ai_discovery_baseline", token_ids)
        baseline_ids = [baseline.id for baseline in baselines.values()]
        ai_highs = {}
        if baseline_ids:
            ai_highs = dict(
                db.query(
                    MarketObservation.token_id,
                    func.max(MarketObservation.market_cap),
                )
                .join(
                    AIIntelligenceRecord,
                    and_(
                        AIIntelligenceRecord.token_id == MarketObservation.token_id,
                        AIIntelligenceRecord.record_type == "ai_discovery_baseline",
                        AIIntelligenceRecord.source == "ai_discovery",
                    ),
                )
                .filter(
                    AIIntelligenceRecord.id.in_(baseline_ids),
                    MarketObservation.is_valid.is_(True),
                    MarketObservation.market_cap.is_not(None),
                    MarketObservation.observed_at >= AIIntelligenceRecord.created_at,
                )
                .group_by(MarketObservation.token_id)
                .all()
            )
        favorite_ids = {
            token_id for (token_id,) in db.query(Favorite.token_id)
            .filter(Favorite.token_id.in_(token_ids))
            .all()
        }
        candidate_records = (
            db.query(AIIntelligenceRecord)
            .filter(
                AIIntelligenceRecord.token_id.in_(latest_exclusions),
                AIIntelligenceRecord.record_type == "ai_discovery_candidate",
                AIIntelligenceRecord.source == "ai_discovery",
            )
            .order_by(AIIntelligenceRecord.id.desc())
            .all()
        )
        latest_candidates = {}
        for record in candidate_records:
            if record.token_id not in latest_candidates and isinstance(record.data, dict):
                latest_candidates[record.token_id] = record.data

        analyzed = []
        for token_id, exclusion in latest_exclusions.items():
            token = db.get(Token, token_id)
            if token is None:
                continue
            exclusion_data = exclusion.data if isinstance(exclusion.data, dict) else {}
            candidate = exclusion_data.get("candidate") or latest_candidates.get(token_id, {})
            display_data = display_data_by_token.get(token_id, {})
            baseline = baselines.get(token_id)
            baseline_data = baseline.data if baseline and isinstance(baseline.data, dict) else {}
            analyzed.append({
                **candidate,
                **display_data,
                "token_id": token_id,
                "name": token.name or display_data.get("name") or candidate.get("name"),
                "ticker": token.ticker or display_data.get("ticker") or candidate.get("ticker"),
                "chain_name": display_data.get("chain_name") or token.chain.chain_name,
                "contract_address": token.contract_address,
                "is_favorite": token_id in favorite_ids,
                "ai_discovery_since": _datetime_iso_utc(baseline.created_at) if baseline else candidate.get("ai_discovery_since"),
                "ai_rmc": baseline_data.get("ai_rmc", candidate.get("ai_rmc")),
                "ai_rh": max(
                    (value for value in (
                        baseline_data.get("ai_rh"),
                        baseline_data.get("ai_rmc"),
                        candidate.get("ai_rh"),
                        ai_highs.get(token_id),
                        display_data.get("market_cap"),
                    ) if value is not None),
                    default=None,
                ),
                "ai_analyzed_at": exclusion_data.get("excluded_at") or _datetime_iso_utc(exclusion.created_at),
                "archive_reason": exclusion_data.get("reason", "manually_removed"),
            })
        return analyzed

    def _excluded_candidate_ids(self, db: Session, token_ids: list[int]) -> set[int]:
        if not token_ids:
            return set()
        return {
            token_id for (token_id,) in db.query(AIIntelligenceRecord.token_id)
            .filter(
                AIIntelligenceRecord.token_id.in_(token_ids),
                AIIntelligenceRecord.record_type == "ai_discovery_exclusion",
                AIIntelligenceRecord.source == "ai_discovery",
            )
            .all()
            if token_id is not None
        }

    def _records_by_token(self, db: Session, record_type: str, token_ids: list[int]) -> dict[int, AIIntelligenceRecord]:
        if not token_ids:
            return {}
        records = (
            db.query(AIIntelligenceRecord)
            .filter(
                AIIntelligenceRecord.record_type == record_type,
                AIIntelligenceRecord.source == "ai_discovery",
                AIIntelligenceRecord.token_id.in_(token_ids),
            )
            .order_by(AIIntelligenceRecord.id.asc())
            .all()
        )
        return {record.token_id: record for record in records}

    def _latest_feedback(self, db: Session, token_ids: list[int]) -> dict[int, str]:
        if not token_ids:
            return {}
        records = (
            db.query(AIIntelligenceRecord)
            .filter(
                AIIntelligenceRecord.record_type == "ai_discovery_feedback",
                AIIntelligenceRecord.token_id.in_(token_ids),
            )
            .order_by(AIIntelligenceRecord.id.desc())
            .all()
        )
        feedback = {}
        for record in records:
            if record.token_id not in feedback and isinstance(record.data, dict):
                feedback[record.token_id] = record.data.get("rating")
        return feedback

    def _refresh_automatic_feedback(
        self,
        db: Session,
        candidates: list[dict[str, Any]],
        baselines: dict[int, AIIntelligenceRecord],
    ) -> dict[int, str]:
        feedback = self._latest_feedback(db, [candidate["token_id"] for candidate in candidates])
        baseline_ids = [baseline.id for baseline in baselines.values()]
        observed_highs = {}
        if baseline_ids:
            observed_highs = dict(
                db.query(
                    MarketObservation.token_id,
                    func.max(MarketObservation.market_cap),
                )
                .join(
                    AIIntelligenceRecord,
                    and_(
                        AIIntelligenceRecord.token_id == MarketObservation.token_id,
                        AIIntelligenceRecord.record_type == "ai_discovery_baseline",
                        AIIntelligenceRecord.source == "ai_discovery",
                    ),
                )
                .filter(
                    AIIntelligenceRecord.id.in_(baseline_ids),
                    MarketObservation.is_valid.is_(True),
                    MarketObservation.market_cap.is_not(None),
                    MarketObservation.observed_at >= AIIntelligenceRecord.created_at,
                )
                .group_by(MarketObservation.token_id)
                .all()
            )
        for candidate in candidates:
            token_id = candidate["token_id"]
            baseline = baselines.get(token_id)
            baseline_data = baseline.data if baseline and isinstance(baseline.data, dict) else {}
            ai_rmc = baseline_data.get("ai_rmc")
            current_market_cap = candidate.get("market_cap")
            if not ai_rmc or current_market_cap is None:
                continue

            observed_high = max(
                current_market_cap,
                observed_highs.get(token_id) or ai_rmc,
            )
            previous_rating = feedback.get(token_id)
            if observed_high >= ai_rmc * 3:
                rating = "positive"
                reason = "observed_high_reached_3x_ai_rmc"
            elif current_market_cap < ai_rmc and previous_rating != "positive":
                rating = "negative"
                reason = "market_cap_fell_below_ai_rmc"
            else:
                continue

            if previous_rating == rating:
                continue
            db.add(AIIntelligenceRecord(
                token_id=token_id,
                record_type="ai_discovery_feedback",
                source="ai_discovery_auto",
                manually_entered=False,
                data={
                    "rating": rating,
                    "reason": reason,
                    "ai_rmc": ai_rmc,
                    "market_cap": current_market_cap,
                    "observed_high": observed_high,
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                },
            ))
            feedback[token_id] = rating
        return feedback

    def _active_top_picks(self, db: Session) -> list[dict[str, Any]]:
        records = (
            db.query(AIIntelligenceRecord)
            .filter(
                AIIntelligenceRecord.record_type == "ai_discovery_top_pick",
                AIIntelligenceRecord.source == "ai_discovery",
            )
            .order_by(AIIntelligenceRecord.id.asc())
            .all()
        )
        picks = []
        for record in records:
            data = record.data if isinstance(record.data, dict) else {}
            if data.get("active"):
                picks.append({**data, "token_id": record.token_id})
        baselines = self._records_by_token(db, "ai_discovery_baseline", [pick["token_id"] for pick in picks])
        for pick in picks:
            baseline = baselines.get(pick["token_id"])
            baseline_data = baseline.data if baseline and isinstance(baseline.data, dict) else {}
            pick["ai_rh"] = max(
                (value for value in (
                    pick.get("ai_rh"),
                    baseline_data.get("ai_rh"),
                    baseline_data.get("ai_rmc"),
                    pick.get("market_cap"),
                ) if value is not None),
                default=None,
            )
        return picks[:MAX_TOP_PICKS]

    def _update_top_picks(
        self,
        db: Session,
        candidates: list[dict[str, Any]],
        feature_by_id: dict[int, dict[str, Any]],
    ) -> None:
        records = (
            db.query(AIIntelligenceRecord)
            .filter(
                AIIntelligenceRecord.record_type == "ai_discovery_top_pick",
                AIIntelligenceRecord.source == "ai_discovery",
            )
            .order_by(AIIntelligenceRecord.id.asc())
            .all()
        )
        records_by_token = {record.token_id: record for record in records}
        token_ids_to_check = {candidate["token_id"] for candidate in candidates}
        token_ids_to_check.update(
            record.token_id for record in records
            if isinstance(record.data, dict) and record.data.get("active")
        )
        active_token_ids = list_active_token_ids(db, token_ids_to_check)
        active_records = [
            record for record in records
            if isinstance(record.data, dict) and record.data.get("active")
        ]
        for record in active_records:
            data = dict(record.data)
            if record.token_id not in active_token_ids:
                data["active"] = False
                data["outcome"] = "no_longer_active"
                data["completed_at"] = datetime.now(timezone.utc).isoformat()
                record.data = data
                continue
            current = feature_by_id.get(record.token_id) or next(
                (candidate for candidate in candidates if candidate["token_id"] == record.token_id),
                None,
            )
            current = dict(current or {})
            latest_market_cap = (
                db.query(MarketObservation.market_cap)
                .filter(
                    MarketObservation.token_id == record.token_id,
                    MarketObservation.is_valid.is_(True),
                    MarketObservation.market_cap.is_not(None),
                )
                .order_by(MarketObservation.observed_at.desc(), MarketObservation.id.desc())
                .limit(1)
                .scalar()
            )
            if latest_market_cap is not None:
                current["market_cap"] = latest_market_cap
            if current.get("market_cap") is not None:
                data["market_cap"] = current["market_cap"]
            if current.get("reached_high_market_cap") is not None:
                data["reached_high_market_cap"] = current["reached_high_market_cap"]
            if current.get("growth_multiple") is not None:
                data["growth_multiple"] = current["growth_multiple"]
            if data.get("t_rmc") is None:
                entry_market_cap = (
                    db.query(MarketObservation.market_cap)
                    .filter(
                        MarketObservation.token_id == record.token_id,
                        MarketObservation.is_valid.is_(True),
                        MarketObservation.market_cap.is_not(None),
                        MarketObservation.observed_at <= record.created_at,
                    )
                    .order_by(MarketObservation.observed_at.desc(), MarketObservation.id.desc())
                    .limit(1)
                    .scalar()
                )
                data["t_rmc"] = entry_market_cap or data.get("market_cap") or current.get("market_cap")
            top_pick_high = (
                db.query(func.max(MarketObservation.market_cap))
                .filter(
                    MarketObservation.token_id == record.token_id,
                    MarketObservation.is_valid.is_(True),
                    MarketObservation.market_cap.is_not(None),
                    MarketObservation.observed_at > record.created_at,
                )
                .scalar()
            )
            data["t_rh"] = max(
                (value for value in (
                    data.get("t_rh"),
                    data.get("t_rmc"),
                    current.get("market_cap"),
                    top_pick_high,
                ) if value is not None),
                default=None,
            )
            if current:
                if current.get("ai_rh") is not None:
                    data["ai_rh"] = max(data.get("ai_rh") or current["ai_rh"], current["ai_rh"])
                ai_rmc = data.get("ai_rmc")
                data["ai_growth_multiple"] = (
                    round(current["market_cap"] / ai_rmc, 3)
                    if current.get("market_cap") is not None and ai_rmc else None
                )
                data["score"] = next(
                    (candidate["score"] for candidate in candidates if candidate["token_id"] == record.token_id),
                    data.get("score"),
                )
                peak = max(
                    (value for value in (data.get("market_cap"), data.get("reached_high_market_cap"), data.get("peak_market_cap")) if value is not None),
                    default=None,
                )
                data["peak_market_cap"] = peak
                if ai_rmc and peak is not None and peak >= ai_rmc * 10:
                    data["active"] = False
                    data["outcome"] = "10x_reached"
                    data["completed_at"] = datetime.now(timezone.utc).isoformat()
            record.data = data

        active_count = sum(
            1 for record in active_records
            if isinstance(record.data, dict) and record.data.get("active")
        )
        if active_count >= MAX_TOP_PICKS:
            return
        for candidate in candidates:
            if active_count >= MAX_TOP_PICKS:
                break
            token_id = candidate["token_id"]
            if token_id not in active_token_ids:
                continue
            if token_id in records_by_token:
                continue
            ai_rmc = candidate.get("ai_rmc")
            ai_growth = candidate.get("ai_growth_multiple")
            if (
                candidate.get("score", 0) < 50
                or not ai_rmc
                or ai_growth is None
                or not (1 < ai_growth <= 2)
                or candidate.get("growth_multiple") is None
                or candidate["growth_multiple"] <= 1
                or candidate["growth_multiple"] > 2
            ):
                continue
            pick = {
                **candidate,
                "active": True,
                "picked_at": candidate.get("last_run_at"),
                "t_rmc": candidate.get("market_cap"),
                "t_rh": candidate.get("market_cap"),
                "peak_market_cap": candidate.get("market_cap"),
                "outcome": "watching",
            }
            db.add(AIIntelligenceRecord(
                token_id=token_id,
                record_type="ai_discovery_top_pick",
                source="ai_discovery",
                manually_entered=False,
                data=pick,
            ))
            records_by_token[token_id] = True
            active_count += 1

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


def _datetime_iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _recommendation(score: float) -> str:
    if score >= 70:
        return "High potential"
    if score >= 50:
        return "Review"
    return "Low signal"


def _continuation_adjustment(growth_multiple: float | None, was_previously_ranked: bool) -> float:
    if not was_previously_ranked or growth_multiple is None:
        return 0.0
    if growth_multiple >= 1:
        return round(min(10.0, 2.0 + (growth_multiple - 1) * 20), 1)
    return round(-min(10.0, (1 - growth_multiple) * 30), 1)