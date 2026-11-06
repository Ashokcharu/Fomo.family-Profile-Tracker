from __future__ import annotations

import json
import hashlib
from math import sqrt
from datetime import date, datetime, timedelta, timezone
from time import monotonic
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import SessionLocal
from app.features.ai_discovery.candidate_features import (
    AI_DISCOVERY_ELIGIBILITY_VERSION,
    build_candidate_features,
)
from app.features.ai_discovery.risk import build_risk_assessments
from app.features.market_data.service import confirmed_market_cap_peaks
from app.features.tokens.service import list_active_token_ids, list_all_active_tokens, token_display_data
from app.integrations.ollama.client import OllamaClient
from app.integrations.ollama.prompts import build_discovery_prompt
from app.models import (
    AIDiscoveryModelAssessment,
    AIDiscoveryOutcome,
    AIDiscoverySnapshot,
    AIIntelligenceRecord,
    BackgroundJob,
    Favorite,
    MarketObservation,
    Token,
)

MAX_MODEL_CANDIDATES = 1
MAX_AI_CANDIDATES = 50
MAX_TOP_PICKS = 3
FEEDBACK_ADJUSTMENT = 8.0
MODEL_AVAILABILITY_CACHE_SECONDS = 300
OUTCOME_HORIZONS_HOURS = (1, 6, 24)
MAX_OUTCOME_SNAPSHOTS_PER_HORIZON = 500
EVALUATION_WINDOW_DAYS = 30
EVALUATION_HOLDOUT_DAYS = 14
EVALUATION_MIN_HOLDOUT_DAYS = 10
EVALUATION_MIN_RISK_COVERAGE_PERCENT = 50.0
EVALUATION_CACHE_SECONDS = 300


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
        self._outcome_summary_checked_at = 0.0
        self._outcome_summary_cache: list[dict[str, Any]] = []
        self._evaluation_report_checked_at = 0.0
        self._evaluation_report_cache: dict[str, Any] | None = None

    def _model_available(self) -> bool:
        now = monotonic()
        if now - self._model_availability_checked_at >= MODEL_AVAILABILITY_CACHE_SECONDS:
            self._model_availability_cache = self.ollama.is_available()
            self._model_availability_checked_at = now
        return self._model_availability_cache

    def _invalidate_outcome_caches(self) -> None:
        self._outcome_summary_checked_at = 0.0
        self._evaluation_report_checked_at = 0.0
        self._evaluation_report_cache = None

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
            candidate_ids = [candidate["token_id"] for candidate in candidates]
            excluded_ids = self._excluded_candidate_ids(db, [candidate["token_id"] for candidate in candidates])
            active_ids = list_active_token_ids(db, set(candidate_ids))
            current_data = token_display_data(db, candidate_ids)
            eligible_candidates = []
            for candidate in candidates:
                token_id = candidate["token_id"]
                current = current_data.get(token_id)
                if (
                    token_id in excluded_ids
                    or token_id not in active_ids
                    or current is None
                    or current.get("growth_percent") is None
                    or current["growth_percent"] < self.settings.ai_discovery_min_growth_percent
                    or current.get("liquidity") is None
                    or current["liquidity"] < self.settings.ai_discovery_min_liquidity_usd
                ):
                    continue
                candidate = {
                    **candidate,
                    "name": current.get("name") or candidate.get("name"),
                    "ticker": current.get("ticker") or candidate.get("ticker"),
                    "contract_address": current.get("contract_address") or candidate.get("contract_address"),
                    "image_url": current.get("image_url") or candidate.get("image_url"),
                    "market_cap": current["market_cap"],
                    "growth_multiple": current["growth_multiple"],
                    "growth_percent": current["growth_percent"],
                    "liquidity": current["liquidity"],
                    "fomo_holder_count": current["fomo_holder_count"],
                    "buyer_count": current["buyer_count"],
                }
                eligible_candidates.append(candidate)
            candidates = eligible_candidates
        if candidates:
            candidate_ids = [candidate["token_id"] for candidate in candidates]
            baselines = self._records_by_token(db, "ai_discovery_baseline", candidate_ids)
            confirmed_ai_highs = self._confirmed_ai_highs(db, candidate_ids, baselines)
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
                candidate["ai_rh"] = confirmed_ai_highs.get(candidate["token_id"])
                if baseline and baseline_data.get("ai_rh") != candidate["ai_rh"]:
                    baseline.data = {**baseline_data, "ai_rh": candidate["ai_rh"]}
                rating = feedback.get(candidate["token_id"])
                adjustment = FEEDBACK_ADJUSTMENT if rating == "positive" else -FEEDBACK_ADJUSTMENT if rating == "negative" else 0.0
                base_score = candidate["heuristic_score"]
                candidate = {
                    **candidate,
                    "feedback": rating,
                    "feedback_adjustment": adjustment,
                    "score": round(max(
                        0.0,
                        min(100.0, base_score + adjustment + candidate.get("continuation_adjustment", 0)),
                    ), 1),
                    "is_favorite": candidate["token_id"] in favorited_ids,
                    "ai_discovery_since": (
                        candidate.get("ai_discovery_since")
                        or (_datetime_iso_utc(baseline.created_at) if baseline else candidate.get("last_run_at"))
                    ),
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
            self._persist_candidate_ai_highs(db, candidates)
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
            "candidate_count": len(candidates),
            "last_run_at": latest_run["last_run_at"] if latest_run else None,
            "candidates": candidates,
            "top_picks": top_picks,
            "outcome_summary": self._outcome_summary(db),
            "evaluation_report": self._evaluation_report(db),
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

        run_datetime = datetime.now(timezone.utc)
        run_at = run_datetime.isoformat()
        tracked_tokens = list_all_active_tokens(db)
        excluded_ids = self._excluded_candidate_ids(db, [token["id"] for token in tracked_tokens])
        tracked_tokens = [token for token in tracked_tokens if token["id"] not in excluded_ids]
        candidate_ids = [token["id"] for token in tracked_tokens]
        baselines = self._records_by_token(db, "ai_discovery_baseline", candidate_ids)
        prior_candidate_ids = {
            token_id for (token_id,) in db.query(AIIntelligenceRecord.token_id)
            .filter(
                AIIntelligenceRecord.token_id.in_(candidate_ids),
                AIIntelligenceRecord.record_type == "ai_discovery_candidate",
                AIIntelligenceRecord.source == "ai_discovery",
            )
            .distinct()
            .all()
            if token_id is not None
        } if candidate_ids else set()
        prior_run = self._latest_run(db)
        prior_candidates = {
            candidate["token_id"]: candidate
            for candidate in (prior_run or {}).get("candidates", [])
        }
        token_features = []
        minimum_growth = self.settings.ai_discovery_min_growth_percent
        for token in tracked_tokens:
            token_id = token["id"]
            baseline = baselines.get(token_id)
            baseline_data = baseline.data if baseline and isinstance(baseline.data, dict) else {}
            market_cap = token.get("market_cap")
            ai_rmc = baseline_data.get("ai_rmc")
            tracked_growth = token.get("growth_multiple")
            tracked_growth_percent = token.get("growth_percent")
            if tracked_growth is None and tracked_growth_percent is not None:
                tracked_growth = 1 + tracked_growth_percent / 100
            eligible = (
                tracked_growth_percent is not None
                and tracked_growth_percent >= minimum_growth
                and market_cap is not None
                and market_cap > 0
                and token.get("liquidity") is not None
                and token["liquidity"] >= self.settings.ai_discovery_min_liquidity_usd
            )
            if eligible and token_id not in prior_candidates:
                ai_rmc = market_cap
                baseline_data = {"ai_rmc": ai_rmc, "ai_rh": market_cap}
                if baseline is None:
                    baseline = AIIntelligenceRecord(
                        token_id=token_id,
                        record_type="ai_discovery_baseline",
                        source="ai_discovery",
                        manually_entered=False,
                        created_at=run_datetime,
                        data=baseline_data,
                    )
                    db.add(baseline)
                    baselines[token_id] = baseline
                else:
                    baseline.created_at = run_datetime
                    baseline.data = baseline_data
            ai_growth = (
                market_cap / ai_rmc
                if market_cap is not None and ai_rmc and ai_rmc > 0 else None
            )
            feature_input = {
                **token,
                "growth_multiple": tracked_growth,
                "growth_percent": tracked_growth_percent,
            }
            candidate = build_candidate_features(feature_input, self.settings)
            candidate["ai_rmc"] = ai_rmc
            candidate["ai_rh"] = max(
                (value for value in (baseline_data.get("ai_rh"), ai_rmc, market_cap) if value is not None),
                default=None,
            )
            candidate["ai_growth_multiple"] = ai_growth
            candidate["growth_multiple"] = tracked_growth
            candidate["growth_percent"] = tracked_growth_percent
            token_features.append(candidate)

        feature_by_id = {candidate["token_id"]: candidate for candidate in token_features}
        feedback_candidates = [
            candidate for candidate in token_features
            if candidate["token_id"] in prior_candidate_ids
            or (
                candidate.get("growth_percent") is not None
                and candidate["growth_percent"] >= minimum_growth
            )
        ]
        feedback = self._refresh_automatic_feedback(db, feedback_candidates, baselines)
        for candidate in token_features:
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
        eligible_features = [
            candidate for candidate in token_features
            if candidate["growth_percent"] is not None
            and candidate["growth_percent"] >= minimum_growth
            and candidate["liquidity"] is not None
            and candidate["liquidity"] >= self.settings.ai_discovery_min_liquidity_usd
        ]
        risk_by_token = build_risk_assessments(
            db,
            [candidate["token_id"] for candidate in eligible_features],
            self.settings,
        )
        for candidate in eligible_features:
            risk = risk_by_token[candidate["token_id"]]
            candidate["risk_assessment"] = risk
            candidate["risk_score"] = risk["risk_score"]
            candidate["risk_level"] = risk["risk_level"]
            candidate["risk_coverage_percent"] = risk["risk_coverage_percent"]
            candidate["data_completeness_percent"] = risk["data_completeness_percent"]
            candidate["data_completeness_missing"] = risk["data_completeness_missing"]
            candidate["risk_assessed_at"] = risk["observed_at"]
        candidates = sorted(
            eligible_features,
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
            confirmed_ai_highs = self._confirmed_ai_highs(db, ai_tracking_ids, baselines)
            for token_id in ai_tracking_ids:
                baseline = baselines[token_id]
                baseline_data = baseline.data if isinstance(baseline.data, dict) else {}
                candidate = feature_by_id[token_id]
                candidate["ai_rh"] = confirmed_ai_highs[token_id]
                baseline.data = {**baseline_data, "ai_rh": candidate["ai_rh"]}
        run_id = uuid4().hex
        model_candidate = candidates[0] if candidates else None
        cached_assessment = None
        if model_candidate is not None:
            previous = prior_candidates.get(model_candidate["token_id"], {})
            model_input = _model_assessment_input(model_candidate)
            if (
                previous.get("model_score") is not None
                and previous.get("model_assessment_input") == model_input
            ):
                cached_assessment = {
                    "score": previous["model_score"],
                    "summary": previous.get("model_summary"),
                    "review": previous.get("model_review"),
                    "assessed_at": previous.get("model_assessed_at"),
                }
        model_available = self._model_available() if use_model and candidates else False
        model_pending = (
            defer_model and model_available and bool(candidates)
            and cached_assessment is None
        )
        if cached_assessment is not None and model_candidate is not None:
            assessments = {model_candidate["token_id"]: cached_assessment}
        elif model_available and candidates and not defer_model:
            assessments = self._get_model_assessments(candidates[:MAX_MODEL_CANDIDATES])
        else:
            assessments = {}
        model_used = bool(assessments)

        stored_candidates = []
        for candidate in candidates:
            assessment = assessments.get(candidate["token_id"], {})
            previous_candidate = prior_candidates.get(candidate["token_id"])
            baseline = baselines.get(candidate["token_id"])
            ai_discovery_since = (
                previous_candidate.get("ai_discovery_since")
                if previous_candidate is not None
                else None
            )
            if ai_discovery_since is None:
                ai_discovery_since = (
                    _datetime_iso_utc(baseline.created_at)
                    if previous_candidate is not None and baseline is not None
                    else run_at
                )
            model_score = assessment.get("score")
            score = candidate["selection_score"]
            score = round(max(0.0, min(100.0, score)), 1)
            if score >= 70:
                recommendation = "High potential"
            elif score >= 50:
                recommendation = "Review"
            else:
                recommendation = "Low signal"
            summary = _fallback_summary(candidate)
            saved = {
                **candidate,
                "eligibility_version": AI_DISCOVERY_ELIGIBILITY_VERSION,
                "score": score,
                "inactive": False,
                "model_score": model_score,
                "model_summary": assessment.get("summary"),
                "model_review": assessment.get("review"),
                "model_assessed_at": assessment.get("assessed_at"),
                "recommendation": recommendation,
                "summary": summary,
                "model": self.settings.ollama_model if model_score is not None else None,
                "model_pending": model_pending,
                "review_mode": "signal_only" if not use_model else "ollama_advisory",
                "model_assessment_input": (
                    _model_assessment_input(candidate)
                    if model_score is not None or (model_pending and candidate is model_candidate)
                    else None
                ),
                "run_id": run_id,
                "last_run_at": run_at,
                "ai_discovery_since": ai_discovery_since,
                "tokens_scanned": len(tracked_tokens),
            }
            stored_candidates.append(saved)
        stored_candidates.sort(
            key=lambda row: (row["score"], row["heuristic_score"], row["token_id"]),
            reverse=True,
        )
        for rank, candidate in enumerate(stored_candidates, start=1):
            candidate["rank"] = rank
        active_candidates = stored_candidates
        active_candidate_ids = {candidate["token_id"] for candidate in active_candidates}
        for token_id, candidate in prior_candidates.items():
            if token_id in active_candidate_ids or token_id in excluded_ids:
                continue
            feature = feature_by_id.get(token_id)
            if feature is None:
                reason = "no_longer_eligible"
            elif (
                feature.get("liquidity") is None
                or feature["liquidity"] < self.settings.ai_discovery_min_liquidity_usd
            ):
                reason = "liquidity_below_threshold"
            elif feature.get("growth_percent") is None or feature["growth_percent"] < minimum_growth:
                reason = "growth_below_threshold"
            elif feature.get("market_cap") is not None and feature["market_cap"] > 0:
                reason = "outside_candidate_limit"
            else:
                reason = "no_longer_eligible"
            self._archive_candidate(db, token_id, candidate, reason)

        stored_candidates = active_candidates
        for rank, candidate in enumerate(stored_candidates, start=1):
            assessment = candidate.get("risk_assessment") or {}
            captured_at = datetime.fromisoformat(candidate["last_run_at"])
            market_data_observed_at = assessment.get("observed_at")
            snapshot = AIDiscoverySnapshot(
                token_id=candidate["token_id"],
                run_id=run_id,
                captured_at=captured_at,
                rank=rank,
                signal_score=candidate["score"],
                market_cap=candidate.get("market_cap"),
                liquidity=candidate.get("liquidity"),
                volume=_number_or_none(assessment.get("volume")),
                price=_number_or_none(assessment.get("price")),
                risk_score=candidate.get("risk_score"),
                data_completeness=candidate.get("data_completeness_percent", 0.0),
                market_data_source=assessment.get("data_source"),
                market_data_observed_at=(
                    datetime.fromisoformat(market_data_observed_at)
                    if market_data_observed_at
                    else None
                ),
                risk_assessment=assessment,
                signal_features={
                    "growth_percent": candidate.get("growth_percent"),
                    "growth_multiple": candidate.get("growth_multiple"),
                    "heuristic_score": candidate.get("heuristic_score"),
                    "score_breakdown": candidate.get("score_breakdown"),
                    "feedback_adjustment": candidate.get("feedback_adjustment"),
                    "continuation_adjustment": candidate.get("continuation_adjustment"),
                },
            )
            db.add(snapshot)
            db.flush()
            candidate["snapshot_id"] = snapshot.id
            db.add(AIIntelligenceRecord(
                token_id=candidate["token_id"],
                record_type="ai_discovery_candidate",
                source="ai_discovery",
                manually_entered=False,
                data=candidate,
            ))
            if candidate.get("model_score") is not None:
                self._save_model_assessment(db, snapshot.id, candidate)
        self._record_due_outcomes(db, datetime.now(timezone.utc))
        self._update_top_picks(db, stored_candidates, feature_by_id)
        db.commit()
        self._invalidate_outcome_caches()

        if not tracked_tokens:
            status = "no_tracked_tokens"
            note = "No active tokens are currently listed in The Tokens tab."
        elif model_pending:
            status = "model_pending"
            note = f"Ranked {len(tracked_tokens)} active tracked tokens using observable signals. {self.settings.ollama_model} is assessing the top candidate in the background."
        elif model_used:
            status = "model_assisted"
            note = f"Ranked {len(tracked_tokens)} active tracked tokens using deterministic signals; Ollama's review of the top candidate is advisory and does not affect rank."
        elif not use_model:
            status = "signal_only"
            note = f"Showing {len(stored_candidates)} growth-eligible candidates (maximum {MAX_AI_CANDIDATES}) using deterministic signals; Ollama was not called."
        else:
            status = "heuristic_fallback"
            note = f"Showing {len(stored_candidates)} growth-eligible candidates (maximum {MAX_AI_CANDIDATES}) from {len(tracked_tokens)} active tracked tokens; Ollama was unavailable or returned no valid assessments."
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
                    AIIntelligenceRecord.data["run_id"].as_string() == run_id,
                )
                .order_by(AIIntelligenceRecord.id.desc())
                .all()
            )
            if not records:
                return
            run_records = records
            run_records.sort(key=lambda record: record.data.get("rank", 0))
            assessments = self._get_model_assessments([run_records[0].data])
            assessment = assessments.get(run_records[0].token_id)
            for record in run_records:
                data = dict(record.data or {})
                data["model_pending"] = False
                if record is run_records[0] and assessment:
                    model_score = assessment["score"]
                    data["model_score"] = model_score
                    data["model_summary"] = assessment.get("summary")
                    data["model_review"] = assessment.get("review")
                    data["model_assessed_at"] = assessment.get("assessed_at")
                    data["model"] = self.settings.ollama_model
                    data["model_assessment_input"] = _model_assessment_input(data)
                    snapshot_id = data.get("snapshot_id")
                    if snapshot_id is not None:
                        self._save_model_assessment(db, int(snapshot_id), data)
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
            self._invalidate_outcome_caches()

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

    def list_analyzed_candidates(self, db: Session) -> list[dict[str, Any]]:
        excluded = (
            db.query(AIIntelligenceRecord)
            .filter(
                AIIntelligenceRecord.record_type == "ai_discovery_exclusion",
                AIIntelligenceRecord.source == "ai_discovery",
            )
            .order_by(AIIntelligenceRecord.id.desc())
            .all()
        )
        confirmed_ai_highs = self._confirmed_ai_highs(
            db,
            [record.token_id for record in excluded if record.token_id is not None],
        )
        results: list[dict[str, Any]] = []
        for record in excluded:
            if record.token_id is None:
                continue
            data = record.data if isinstance(record.data, dict) else {}
            candidate_data = data.get("candidate") if isinstance(data.get("candidate"), dict) else {}
            latest_candidate = (
                db.query(AIIntelligenceRecord)
                .filter(
                    AIIntelligenceRecord.token_id == record.token_id,
                    AIIntelligenceRecord.record_type == "ai_discovery_candidate",
                    AIIntelligenceRecord.source == "ai_discovery",
                )
                .order_by(AIIntelligenceRecord.id.desc())
                .first()
            )
            current_candidate = latest_candidate.data if latest_candidate and isinstance(latest_candidate.data, dict) else {}
            merged = {**current_candidate, **candidate_data}

            latest_market = (
                db.query(MarketObservation)
                .filter(
                    MarketObservation.token_id == record.token_id,
                    MarketObservation.is_valid.is_(True),
                    MarketObservation.market_cap.is_not(None),
                )
                .order_by(MarketObservation.observed_at.desc(), MarketObservation.id.desc())
                .first()
            )
            if latest_market is not None:
                merged["market_cap"] = latest_market.market_cap
                merged["liquidity"] = latest_market.liquidity
                merged["price"] = latest_market.price
                merged["source"] = latest_market.source

            ai_rh = confirmed_ai_highs.get(record.token_id)
            merged["ai_rh"] = ai_rh

            results.append({
                "token_id": record.token_id,
                "score": merged.get("score"),
                "market_cap": merged.get("market_cap"),
                "liquidity": merged.get("liquidity"),
                "ai_rh": merged.get("ai_rh"),
                "archive_reason": data.get("reason") or candidate_data.get("archive_reason") or "archived",
                "ai_analyzed_at": merged.get("last_run_at") or record.created_at.isoformat(),
            })
        return results

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
            .order_by(AIIntelligenceRecord.id.desc())
            .first()
        )
        existing_data = exclusion.data if exclusion and isinstance(exclusion.data, dict) else {}
        existing_is_manual = bool(
            exclusion and (
                exclusion.manually_entered or existing_data.get("reason") == "manually_removed"
            )
        )
        if existing_is_manual and reason != "manually_removed":
            return
        archive_data = {
            "excluded_at": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
            "candidate": candidate,
        }
        if exclusion is not None:
            exclusion.manually_entered = reason == "manually_removed"
            exclusion.data = archive_data
        else:
            db.add(AIIntelligenceRecord(
                token_id=token_id,
                record_type="ai_discovery_exclusion",
                source="ai_discovery",
                manually_entered=reason == "manually_removed",
                data=archive_data,
            ))
        latest_candidate = (
            db.query(AIIntelligenceRecord)
            .filter(
                AIIntelligenceRecord.token_id == token_id,
                AIIntelligenceRecord.record_type == "ai_discovery_candidate",
                AIIntelligenceRecord.source == "ai_discovery",
            )
            .order_by(AIIntelligenceRecord.id.desc())
            .first()
        )
        if latest_candidate and isinstance(latest_candidate.data, dict):
            latest_candidate.data = {**latest_candidate.data, "inactive": True}

    def _excluded_candidate_ids(self, db: Session, token_ids: list[int]) -> set[int]:
        if not token_ids:
            return set()
        records = db.query(AIIntelligenceRecord).filter(
            AIIntelligenceRecord.token_id.in_(token_ids),
            AIIntelligenceRecord.record_type == "ai_discovery_exclusion",
            AIIntelligenceRecord.source == "ai_discovery",
        ).all()
        return {
            record.token_id for record in records
            if record.token_id is not None and (
                record.manually_entered
                or (
                    isinstance(record.data, dict)
                    and record.data.get("reason") == "manually_removed"
                )
            )
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

    def _confirmed_ai_highs(
        self,
        db: Session,
        token_ids: set[int] | list[int],
        baselines: dict[int, AIIntelligenceRecord] | None = None,
    ) -> dict[int, float | None]:
        unique_ids = set(token_ids)
        if not unique_ids:
            return {}
        baseline_records = baselines or self._records_by_token(
            db,
            "ai_discovery_baseline",
            list(unique_ids),
        )
        tracked_baselines = {
            token_id: baseline
            for token_id, baseline in baseline_records.items()
            if token_id in unique_ids
        }
        peaks = confirmed_market_cap_peaks(
            db,
            set(tracked_baselines),
            {token_id: baseline.created_at for token_id, baseline in tracked_baselines.items()},
        )
        result: dict[int, float | None] = {token_id: None for token_id in unique_ids}
        for token_id, baseline in tracked_baselines.items():
            data = baseline.data if isinstance(baseline.data, dict) else {}
            ai_rmc = _number_or_none(data.get("ai_rmc"))
            values = [value for value in (ai_rmc, peaks.get(token_id, (None, None))[0]) if value is not None]
            result[token_id] = max(values) if values else None
        return result

    def _persist_candidate_ai_highs(self, db: Session, candidates: list[dict[str, Any]]) -> None:
        for candidate in candidates:
            run_id = candidate.get("run_id")
            if not run_id:
                continue
            record = (
                db.query(AIIntelligenceRecord)
                .filter(
                    AIIntelligenceRecord.token_id == candidate["token_id"],
                    AIIntelligenceRecord.record_type == "ai_discovery_candidate",
                    AIIntelligenceRecord.source == "ai_discovery",
                    AIIntelligenceRecord.data["run_id"].as_string() == run_id,
                )
                .order_by(AIIntelligenceRecord.id.desc())
                .first()
            )
            if record is None or not isinstance(record.data, dict):
                continue
            if record.data.get("ai_rh") != candidate.get("ai_rh"):
                record.data = {**record.data, "ai_rh": candidate.get("ai_rh")}

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
        observed_highs = self._confirmed_ai_highs(
            db,
            [candidate["token_id"] for candidate in candidates],
            baselines,
        )
        for candidate in candidates:
            token_id = candidate["token_id"]
            baseline = baselines.get(token_id)
            baseline_data = baseline.data if baseline and isinstance(baseline.data, dict) else {}
            ai_rmc = baseline_data.get("ai_rmc")
            current_market_cap = candidate.get("market_cap")
            if not ai_rmc or current_market_cap is None:
                continue

            observed_high = observed_highs.get(token_id) or ai_rmc
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
        display_data = token_display_data(db, [pick["token_id"] for pick in picks])
        baselines = self._records_by_token(db, "ai_discovery_baseline", [pick["token_id"] for pick in picks])
        confirmed_ai_highs = self._confirmed_ai_highs(
            db,
            [pick["token_id"] for pick in picks],
            baselines,
        )
        eligible_picks = []
        for pick in picks:
            current = display_data.get(pick["token_id"], {})
            if (
                current.get("liquidity") is None
                or current["liquidity"] < self.settings.ai_discovery_min_liquidity_usd
            ):
                continue
            eligible_picks.append(pick)
            for key in ("name", "ticker", "contract_address", "image_url", "chain_name", "fomo_url"):
                pick[key] = current.get(key) or pick.get(key)
            pick["liquidity"] = current["liquidity"]
            pick["ai_rh"] = confirmed_ai_highs.get(pick["token_id"])
        return eligible_picks[:MAX_TOP_PICKS]

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
        current_market_data = token_display_data(
            db,
            [record.token_id for record in active_records if record.token_id is not None],
        )
        confirmed_ai_highs = self._confirmed_ai_highs(
            db,
            [record.token_id for record in active_records if record.token_id is not None],
        )
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
                current_market_data.get(record.token_id),
            )
            current = dict(current or {})
            if (
                current.get("liquidity") is None
                or current["liquidity"] < self.settings.ai_discovery_min_liquidity_usd
            ):
                data["active"] = False
                data["outcome"] = "liquidity_below_threshold"
                data["completed_at"] = datetime.now(timezone.utc).isoformat()
                record.data = data
                continue
            data["ai_rh"] = (
                current["ai_rh"]
                if current.get("ai_rh") is not None
                else confirmed_ai_highs.get(record.token_id)
            )
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
            _model_assessment_input(candidate)
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
            assessments[token_id] = {
                "score": score,
                "summary": summary[:240] if summary else None,
                "assessed_at": datetime.now(timezone.utc).isoformat(),
                "review": {
                    field: _validated_review_items(item.get(field))
                    for field in ("supporting_evidence", "concerns", "missing_data")
                } | {
                    "uncertainty": (
                        item["uncertainty"][:240]
                        if isinstance(item.get("uncertainty"), str)
                        else None
                    ),
                },
            }
        return assessments

    def _latest_run(self, db: Session) -> dict[str, Any] | None:
        latest_record = (
            db.query(AIIntelligenceRecord)
            .filter(
                AIIntelligenceRecord.record_type == "ai_discovery_candidate",
                AIIntelligenceRecord.source == "ai_discovery",
            )
            .order_by(AIIntelligenceRecord.id.desc())
            .first()
        )
        if latest_record is None or not isinstance(latest_record.data, dict):
            return None
        run_id = latest_record.data.get("run_id")
        if not run_id:
            return None
        run_records = (
            db.query(AIIntelligenceRecord)
            .filter(
                AIIntelligenceRecord.record_type == "ai_discovery_candidate",
                AIIntelligenceRecord.source == "ai_discovery",
                AIIntelligenceRecord.data["run_id"].as_string() == run_id,
            )
            .all()
        )
        run_data = [record.data for record in run_records]
        candidates = [data for data in run_data if not data.get("inactive")]
        candidates.sort(key=lambda row: row.get("rank", 0))
        scanned = int(run_data[0].get("tokens_scanned", 0)) if run_data else 0
        last_run_at = run_data[0].get("last_run_at") if run_data else None
        model_used = any(candidate.get("model_score") is not None for candidate in candidates)
        model_pending = any(candidate.get("model_pending") for candidate in candidates)
        if model_used:
            status = "model_assisted"
            note = f"Ranked {scanned} active tracked tokens using deterministic signals; Ollama's review is advisory and does not affect rank."
        elif model_pending:
            status = "model_pending"
            note = f"Ranked {scanned} active tracked tokens using observable signals. {self.settings.ollama_model} is assessing the top candidate in the background."
        elif candidates and all(candidate.get("review_mode") == "signal_only" for candidate in candidates):
            status = "signal_only"
            note = f"Showing {len(candidates)} growth-eligible candidates using deterministic signals; Ollama was not called."
        else:
            status = "heuristic_fallback"
            note = f"Showing {len(candidates)} growth-eligible candidates (maximum {MAX_AI_CANDIDATES}) from {scanned} active tracked tokens using observable signal scoring."
        return {"candidates": candidates, "tokens_scanned": scanned, "last_run_at": last_run_at, "status": status, "note": note}

    def _record_due_outcomes(self, db: Session, now: datetime) -> None:
        grace = timedelta(seconds=max(900, self.settings.token_refresh_interval_seconds * 2))
        for horizon_hours in OUTCOME_HORIZONS_HOURS:
            due_before = now - timedelta(hours=horizon_hours)
            has_outcome = (
                select(AIDiscoveryOutcome.id)
                .where(
                    AIDiscoveryOutcome.snapshot_id == AIDiscoverySnapshot.id,
                    AIDiscoveryOutcome.horizon_hours == horizon_hours,
                )
                .exists()
            )
            snapshots = (
                db.query(AIDiscoverySnapshot)
                .filter(
                    AIDiscoverySnapshot.captured_at <= due_before,
                    ~has_outcome,
                )
                .order_by(AIDiscoverySnapshot.captured_at.asc(), AIDiscoverySnapshot.id.asc())
                .limit(MAX_OUTCOME_SNAPSHOTS_PER_HORIZON)
                .all()
            )
            for snapshot in snapshots:
                captured_at = snapshot.captured_at
                if captured_at.tzinfo is None:
                    captured_at = captured_at.replace(tzinfo=timezone.utc)
                target_at = captured_at + timedelta(hours=horizon_hours)
                observed = (
                    db.query(MarketObservation)
                    .filter(
                        MarketObservation.token_id == snapshot.token_id,
                        MarketObservation.is_valid.is_(True),
                        MarketObservation.market_cap.is_not(None),
                        MarketObservation.observed_at >= target_at,
                        MarketObservation.observed_at <= target_at + grace,
                    )
                    .order_by(MarketObservation.observed_at.asc(), MarketObservation.id.asc())
                    .first()
                )
                if observed is None and now <= target_at + grace:
                    continue
                end_at = observed.observed_at if observed is not None else target_at
                minimum_cap, maximum_cap = (
                    db.query(
                        func.min(MarketObservation.market_cap),
                        func.max(MarketObservation.market_cap),
                    )
                    .filter(
                        MarketObservation.token_id == snapshot.token_id,
                        MarketObservation.is_valid.is_(True),
                        MarketObservation.market_cap.is_not(None),
                        MarketObservation.observed_at >= captured_at,
                        MarketObservation.observed_at <= end_at,
                    )
                    .one()
                )
                entry_market_cap = snapshot.market_cap
                db.add(AIDiscoveryOutcome(
                    snapshot_id=snapshot.id,
                    horizon_hours=horizon_hours,
                    target_at=target_at,
                    observed_at=observed.observed_at if observed is not None else None,
                    status="observed" if observed is not None and entry_market_cap else "data_unavailable",
                    market_cap=observed.market_cap if observed is not None else None,
                    return_percent=(
                        round((observed.market_cap / entry_market_cap - 1) * 100, 2)
                        if observed is not None and entry_market_cap and entry_market_cap > 0
                        else None
                    ),
                    max_return_percent=(
                        round((maximum_cap / entry_market_cap - 1) * 100, 2)
                        if maximum_cap is not None and entry_market_cap and entry_market_cap > 0
                        else None
                    ),
                    max_drawdown_percent=(
                        round(max(0.0, (1 - minimum_cap / entry_market_cap) * 100), 2)
                        if minimum_cap is not None and entry_market_cap and entry_market_cap > 0
                        else None
                    ),
                    source=(observed.provider_name or observed.source) if observed is not None else None,
                ))

    def _outcome_summary(self, db: Session) -> list[dict[str, Any]]:
        cache_age = monotonic() - self._outcome_summary_checked_at
        if self._outcome_summary_checked_at and cache_age < 300:
            return [dict(summary) for summary in self._outcome_summary_cache]
        now = datetime.now(timezone.utc)
        rows = (
            db.query(
                AIDiscoveryOutcome.horizon_hours,
                AIDiscoveryOutcome.status,
                func.count(AIDiscoveryOutcome.id),
                func.avg(AIDiscoveryOutcome.return_percent),
                func.avg(AIDiscoveryOutcome.max_drawdown_percent),
            )
            .filter(AIDiscoveryOutcome.target_at >= now - timedelta(days=30))
            .group_by(AIDiscoveryOutcome.horizon_hours, AIDiscoveryOutcome.status)
            .all()
        )
        totals: dict[int, dict[str, Any]] = {
            horizon: {
                "horizon_hours": horizon,
                "observed_count": 0,
                "unavailable_count": 0,
                "average_return_percent": None,
                "average_max_drawdown_percent": None,
            }
            for horizon in OUTCOME_HORIZONS_HOURS
        }
        for horizon, status, count, average_return, average_drawdown in rows:
            summary = totals.setdefault(horizon, {
                "horizon_hours": horizon,
                "observed_count": 0,
                "unavailable_count": 0,
                "average_return_percent": None,
                "average_max_drawdown_percent": None,
            })
            summary["observed_count" if status == "observed" else "unavailable_count"] += count
            if status == "observed":
                summary["average_return_percent"] = (
                    round(average_return, 2) if average_return is not None else None
                )
                summary["average_max_drawdown_percent"] = (
                    round(average_drawdown, 2) if average_drawdown is not None else None
                )
        self._outcome_summary_cache = [totals[horizon] for horizon in OUTCOME_HORIZONS_HOURS]
        self._outcome_summary_checked_at = monotonic()
        return [dict(summary) for summary in self._outcome_summary_cache]

    def _save_model_assessment(
        self,
        db: Session,
        snapshot_id: int,
        candidate: dict[str, Any],
    ) -> None:
        assessed_at = candidate.get("model_assessed_at")
        if not isinstance(assessed_at, str):
            raise ValueError("Model assessment is missing its assessment timestamp")
        assessment_input = candidate.get("model_assessment_input") or {}
        assessment_hash = hashlib.sha256(
            json.dumps(assessment_input, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        assessment = db.query(AIDiscoveryModelAssessment).filter_by(snapshot_id=snapshot_id).one_or_none()
        if assessment is None:
            assessment = AIDiscoveryModelAssessment(snapshot_id=snapshot_id)
            db.add(assessment)
        assessment.model_name = candidate.get("model") or self.settings.ollama_model
        assessment.score = float(candidate["model_score"])
        assessment.summary = candidate.get("model_summary")
        assessment.review = candidate.get("model_review")
        assessment.assessment_hash = assessment_hash
        assessment.assessed_at = datetime.fromisoformat(assessed_at)

    def _evaluation_report(self, db: Session) -> dict[str, Any]:
        if (
            self._evaluation_report_cache is not None
            and monotonic() - self._evaluation_report_checked_at < EVALUATION_CACHE_SECONDS
        ):
            return self._evaluation_report_cache

        now = datetime.now(timezone.utc)
        window_start = now - timedelta(days=EVALUATION_WINDOW_DAYS)
        holdout_start = now - timedelta(days=EVALUATION_HOLDOUT_DAYS)
        snapshots = (
            db.query(AIDiscoverySnapshot)
            .filter(
                AIDiscoverySnapshot.captured_at >= window_start,
                AIDiscoverySnapshot.captured_at <= now,
            )
            .order_by(AIDiscoverySnapshot.captured_at.asc(), AIDiscoverySnapshot.id.asc())
            .all()
        )
        snapshot_ids = [snapshot.id for snapshot in snapshots]
        outcomes = (
            db.query(AIDiscoveryOutcome)
            .filter(
                AIDiscoveryOutcome.snapshot_id.in_(snapshot_ids),
                AIDiscoveryOutcome.status == "observed",
                AIDiscoveryOutcome.target_at <= now,
            )
            .all()
            if snapshot_ids
            else []
        )
        assessments = (
            db.query(AIDiscoveryModelAssessment)
            .filter(AIDiscoveryModelAssessment.snapshot_id.in_(snapshot_ids))
            .all()
            if snapshot_ids
            else []
        )
        outcomes_by_snapshot: dict[int, dict[int, AIDiscoveryOutcome]] = {}
        for outcome in outcomes:
            outcomes_by_snapshot.setdefault(outcome.snapshot_id, {})[outcome.horizon_hours] = outcome
        assessments_by_snapshot = {assessment.snapshot_id: assessment for assessment in assessments}
        snapshots_by_run: dict[str, list[AIDiscoverySnapshot]] = {}
        for snapshot in snapshots:
            snapshots_by_run.setdefault(snapshot.run_id, []).append(snapshot)
        runs_by_day: dict[date, list[list[AIDiscoverySnapshot]]] = {}
        for run_snapshots in snapshots_by_run.values():
            run_day = _aware_datetime(run_snapshots[0].captured_at).date()
            runs_by_day.setdefault(run_day, []).append(run_snapshots)
        for day_runs in runs_by_day.values():
            day_runs.sort(key=lambda group: _aware_datetime(group[0].captured_at))

        horizons = []
        for horizon_hours in OUTCOME_HORIZONS_HOURS:
            periods: dict[str, dict[str, Any]] = {
                name: {
                    "signal_values": [],
                    "adjusted_values": [],
                    "signal_drawdowns": [],
                    "adjusted_drawdowns": [],
                    "model_values": [],
                    "model_scores": [],
                    "model_sample_days": [],
                    "paired_differences": [],
                    "paired_drawdown_differences": [],
                    "paired_runs": 0,
                    "risk_covered_snapshots": 0,
                    "total_snapshots": 0,
                }
                for name in ("development", "holdout")
            }
            for run_day, day_runs in sorted(runs_by_day.items()):
                period_name = "holdout" if run_day >= holdout_start.date() else "development"
                period = periods[period_name]
                period["total_snapshots"] += sum(len(run) for run in day_runs)
                period["risk_covered_snapshots"] += sum(
                    1 for run in day_runs for item in run
                    if item.risk_score is not None
                    and _snapshot_risk_coverage(item) >= EVALUATION_MIN_RISK_COVERAGE_PERCENT
                )
                for run_snapshots in day_runs:
                    ranked = sorted(
                        run_snapshots,
                        key=lambda item: (item.signal_score, -item.rank, item.token_id),
                        reverse=True,
                    )
                    covered = [
                        snapshot for snapshot in ranked
                        if snapshot.risk_score is not None
                        and _snapshot_risk_coverage(snapshot) >= EVALUATION_MIN_RISK_COVERAGE_PERCENT
                    ]
                    signal_metrics = _complete_outcome_metrics(
                        covered[:MAX_TOP_PICKS], outcomes_by_snapshot, horizon_hours
                    )
                    adjusted = sorted(
                        covered,
                        key=lambda item: (
                            item.signal_score * (1 - item.risk_score / 200),
                            item.signal_score,
                            -item.rank,
                        ),
                        reverse=True,
                    )[:MAX_TOP_PICKS]
                    adjusted_metrics = _complete_outcome_metrics(
                        adjusted, outcomes_by_snapshot, horizon_hours
                    )
                    if signal_metrics is None or adjusted_metrics is None:
                        continue

                    signal_return = _mean(signal_metrics["returns"])
                    adjusted_return = _mean(adjusted_metrics["returns"])
                    signal_drawdown = _mean(signal_metrics["drawdowns"])
                    adjusted_drawdown = _mean(adjusted_metrics["drawdowns"])
                    period["signal_values"].append(signal_return)
                    period["adjusted_values"].append(adjusted_return)
                    period["signal_drawdowns"].append(signal_drawdown)
                    period["adjusted_drawdowns"].append(adjusted_drawdown)
                    period["paired_differences"].append(adjusted_return - signal_return)
                    period["paired_drawdown_differences"].append(
                        adjusted_drawdown - signal_drawdown
                    )
                    period["paired_runs"] += 1
                    break

                for run_snapshots in day_runs:
                    reviewed = sorted(
                        (
                            snapshot for snapshot in run_snapshots
                            if snapshot.id in assessments_by_snapshot
                            and _aware_datetime(assessments_by_snapshot[snapshot.id].assessed_at)
                            <= _aware_datetime(snapshot.captured_at) + timedelta(hours=horizon_hours)
                        ),
                        key=lambda item: _aware_datetime(assessments_by_snapshot[item.id].assessed_at),
                    )
                    for snapshot in reviewed:
                        outcome = outcomes_by_snapshot.get(snapshot.id, {}).get(horizon_hours)
                        if outcome is None or outcome.return_percent is None:
                            continue
                        assessment = assessments_by_snapshot[snapshot.id]
                        period["model_values"].append(outcome.return_percent)
                        period["model_scores"].append(assessment.score)
                        period["model_sample_days"].append(run_day)
                        break
                    if period["model_sample_days"] and period["model_sample_days"][-1] == run_day:
                        break

            def summarize_period(period: dict[str, Any]) -> dict[str, Any]:
                return {
                    "signal_top3_risk_covered": _evaluation_metrics(
                        period["signal_values"], len(period["signal_values"])
                    ),
                    "risk_adjusted_top3": _evaluation_metrics(
                        period["adjusted_values"], len(period["adjusted_values"])
                    ),
                    "risk_covered_snapshots": period["risk_covered_snapshots"],
                    "total_snapshots": period["total_snapshots"],
                    "paired_run_count": period["paired_runs"],
                    "paired_day_count": period["paired_runs"],
                    "paired_mean_return_delta_percent": (
                        round(_mean(period["paired_differences"]), 2)
                        if period["paired_differences"]
                        else None
                    ),
                    "signal_average_max_drawdown_percent": (
                        round(_mean(period["signal_drawdowns"]), 2)
                        if period["signal_drawdowns"]
                        else None
                    ),
                    "risk_adjusted_average_max_drawdown_percent": (
                        round(_mean(period["adjusted_drawdowns"]), 2)
                        if period["adjusted_drawdowns"]
                        else None
                    ),
                    "paired_mean_max_drawdown_delta_percent": (
                        round(_mean(period["paired_drawdown_differences"]), 2)
                        if period["paired_drawdown_differences"]
                        else None
                    ),
                    "paired_return_delta_95_ci": _paired_mean_confidence_interval(
                        period["paired_differences"]
                    ),
                    "ollama_reviewed": _evaluation_metrics(
                        period["model_values"], len(period["model_values"])
                    ) | {
                        "average_model_score": (
                            round(_mean(period["model_scores"]), 2)
                            if period["model_scores"]
                            else None
                        ),
                    },
                }

            development = summarize_period(periods["development"])
            holdout = summarize_period(periods["holdout"])
            horizons.append({
                "horizon_hours": horizon_hours,
                "development": development,
                "holdout": holdout,
                **holdout,
            })

        promotion_checks = []
        for horizon in horizons:
            holdout = horizon["holdout"]
            interval = holdout["paired_return_delta_95_ci"]
            enough_days = holdout["paired_run_count"] >= EVALUATION_MIN_HOLDOUT_DAYS
            positive_lift = interval["lower_percent"] is not None and interval["lower_percent"] > 0
            drawdown_not_worse = (
                holdout["paired_mean_max_drawdown_delta_percent"] is not None
                and holdout["paired_mean_max_drawdown_delta_percent"] <= 0
            )
            promotion_checks.append(enough_days and positive_lift and drawdown_not_worse)
        if all(promotion_checks):
            promotion_status = "ready_for_manual_review"
            promotion_reason = (
                "Risk-adjusted results improved at every horizon, with the paired 95% confidence "
                "lower bound above zero and no increase in average maximum drawdown."
            )
        elif all(
            horizon["holdout"]["paired_run_count"] >= EVALUATION_MIN_HOLDOUT_DAYS
            for horizon in horizons
        ):
            promotion_status = "no_measurable_improvement"
            promotion_reason = (
                "The holdout does not show positive, uncertainty-adjusted return lift "
                "without increased drawdown at every horizon."
            )
        else:
            promotion_status = "insufficient_evidence"
            promotion_reason = (
                f"At least {EVALUATION_MIN_HOLDOUT_DAYS} complete independent UTC-day cohorts "
                "per horizon are required; production weights remain zero."
            )

        report = {
            "window_days": EVALUATION_WINDOW_DAYS,
            "holdout_days": EVALUATION_HOLDOUT_DAYS,
            "risk_min_coverage_percent": EVALUATION_MIN_RISK_COVERAGE_PERCENT,
            "as_of": now.isoformat(),
            "snapshot_count": len(snapshots),
            "independent_sample_unit": "one earliest complete discovery run per UTC day",
            "ranking_policy": {
                "risk_adjusted_status": promotion_status,
                "reason": promotion_reason,
                "minimum_paired_holdout_days": EVALUATION_MIN_HOLDOUT_DAYS,
                "active_risk_weight": 0,
                "active_ollama_weight": 0,
                "promotion_requires_manual_approval": True,
            },
            "risk_adjusted_method": (
                "signal_score × (1 − risk_score / 200); "
                f"risk scores with at least {EVALUATION_MIN_RISK_COVERAGE_PERCENT:.0f}% factor coverage only"
            ),
            "comparison_note": "Development and holdout windows are chronological. Metrics use one earliest complete cohort per UTC day to limit repeated-scan dependence. This research-only comparison never changes production eligibility or ranking.",
            "model_review_note": "Ollama reviews the signal leader and does not change selection; reviewed outcomes are not evidence of independent model ranking lift.",
            "horizons": horizons,
        }
        self._evaluation_report_cache = report
        self._evaluation_report_checked_at = monotonic()
        return report


def _number_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _snapshot_risk_coverage(snapshot: AIDiscoverySnapshot) -> float:
    assessment = snapshot.risk_assessment if isinstance(snapshot.risk_assessment, dict) else {}
    try:
        return float(assessment.get("risk_coverage_percent") or 0)
    except (TypeError, ValueError):
        return 0.0


def _complete_outcome_metrics(
    snapshots: list[AIDiscoverySnapshot],
    outcomes_by_snapshot: dict[int, dict[int, AIDiscoveryOutcome]],
    horizon_hours: int,
) -> dict[str, list[float]] | None:
    if not snapshots:
        return None
    outcomes = [
        outcomes_by_snapshot.get(snapshot.id, {}).get(horizon_hours)
        for snapshot in snapshots
    ]
    if any(
        outcome is None
        or outcome.return_percent is None
        or outcome.max_drawdown_percent is None
        for outcome in outcomes
    ):
        return None
    return {
        "returns": [float(outcome.return_percent) for outcome in outcomes if outcome is not None],
        "drawdowns": [
            float(outcome.max_drawdown_percent)
            for outcome in outcomes
            if outcome is not None
        ],
    }


def _evaluation_metrics(returns: list[float], covered_runs: int) -> dict[str, Any]:
    return {
        "sample_count": len(returns),
        "complete_cohort_count": covered_runs,
        "average_return_percent": round(sum(returns) / len(returns), 2) if returns else None,
        "win_rate_percent": round(sum(value > 0 for value in returns) / len(returns) * 100, 2) if returns else None,
    }


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _paired_mean_confidence_interval(values: list[float]) -> dict[str, Any]:
    if len(values) < 2:
        return {
            "method": "two-sided 95% Student-t; conservative critical value 2.262",
            "sample_count": len(values),
            "lower_percent": None,
            "upper_percent": None,
        }
    mean = _mean(values)
    sample_variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    margin = 2.262 * sqrt(sample_variance / len(values))
    return {
        "method": "two-sided 95% Student-t; conservative critical value 2.262",
        "sample_count": len(values),
        "lower_percent": round(mean - margin, 2),
        "upper_percent": round(mean + margin, 2),
    }


def _aware_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _fallback_summary(candidate: dict) -> str:
    strengths = "; ".join(candidate["strengths"][:2])
    risks = "; ".join(candidate["risks"][:2])
    parts = [part for part in (strengths, f"Risks: {risks}" if risks else "No tracked risk flags") if part]
    return ". ".join(parts)[:240] or "Insufficient tracked market data for a detailed assessment."


def _model_assessment_input(candidate: dict) -> dict:
    model_input = {
        key: candidate.get(key)
        for key in (
            "token_id",
            "name",
            "ticker",
            "chain_name",
            "market_cap",
            "ai_rmc",
            "ai_growth_multiple",
            "growth_percent",
            "liquidity",
            "buyer_count",
            "fomo_holder_count",
            "heuristic_score",
            "score_breakdown",
            "strengths",
            "risks",
            "risk_assessment",
        )
    }
    risk = candidate.get("risk_assessment")
    if isinstance(risk, dict):
        model_input["risk_assessment"] = {
            "risk_score": risk.get("risk_score"),
            "risk_level": risk.get("risk_level"),
            "risk_coverage_percent": risk.get("risk_coverage_percent"),
            "data_completeness_percent": risk.get("data_completeness_percent"),
            "data_source": risk.get("data_source"),
            "observed_at": risk.get("observed_at"),
            "factors": [
                {
                    key: factor.get(key)
                    for key in ("name", "status", "score", "source", "observed_at")
                }
                for factor in risk.get("factors", [])
                if isinstance(factor, dict)
            ],
        }
    return model_input


def _validated_review_items(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item[:180] for item in value if isinstance(item, str)][:5]


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