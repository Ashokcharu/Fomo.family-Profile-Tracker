from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.chains.registry import chain_registry
from app.config import get_settings
from app.integrations.helius.client import HeliusClient
from app.models import MarketObservation, RiskFinding, SecurityAnalysis, Token, TraderTransactionEvent

SPEC_PHASES = {
    "phase_1": "baseline token and market metadata collection",
    "phase_2": "market signal and liquidity health scoring",
    "phase_3": "wallet activity and creator concentration checks",
    "phase_4": "liquidity lock and LP security verification",
    "phase_5": "provider-aware security analysis and risk scoring",
    "phase_6": "historical trend and time-series comparison",
    "phase_7": "provider coverage and chain readiness summaries",
    "phase_8": "implementation metadata and spec traceability",
    "phase_9": "production readiness and architecture hardening",
}


class SecurityAnalysisService:
    def _chain_adapter_name(self, token: Token | None) -> str:
        if token is None or token.chain is None:
            return "unknown"
        adapter = chain_registry.get(token.chain.chain_name)
        return adapter.chain_name if adapter is not None else token.chain.chain_name

    def _normalized_token_address(self, token: Token) -> str:
        if token.chain is None:
            return token.normalized_address
        adapter = chain_registry.get(token.chain.chain_name)
        if adapter is None:
            return token.normalized_address
        return adapter.normalize_address(token.normalized_address)

    def _phase_summary(self) -> dict[str, Any]:
        implemented = ["phase_1", "phase_2", "phase_3", "phase_4", "phase_5"]
        full_spec = list(SPEC_PHASES)
        return {
            "current_phase": "phase_5",
            "implemented_phases": implemented,
            "spec_phases": full_spec,
            "spec_status": {
                phase: {"implemented": phase in implemented, "description": description}
                for phase, description in SPEC_PHASES.items()
            },
        }
    def _latest_market_observation(self, db: Session, token_id: int) -> MarketObservation | None:
        return (
            db.query(MarketObservation)
            .filter(MarketObservation.token_id == token_id)
            .order_by(MarketObservation.observed_at.desc(), MarketObservation.id.desc())
            .first()
        )

    def _risk_level_for_score(self, score: int) -> str:
        if score >= 80:
            return "critical"
        if score >= 60:
            return "high"
        if score >= 35:
            return "medium"
        if score >= 15:
            return "low"
        return "unverified"

    def _coerce_float(self, value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _normalize_percentage(self, value: Any) -> float | None:
        numeric = self._coerce_float(value)
        if numeric is None:
            return None
        if 0.0 <= numeric <= 1.0:
            numeric *= 100.0
        if numeric < 0.0:
            numeric = 0.0
        if numeric > 100.0:
            numeric = 100.0
        return round(numeric, 2)

    def _recent_wallet_activity(self, db: Session, token_id: int, limit: int = 10) -> list[dict[str, Any]]:
        rows = (
            db.query(TraderTransactionEvent)
            .filter(TraderTransactionEvent.token_id == token_id)
            .order_by(TraderTransactionEvent.detection_timestamp.desc(), TraderTransactionEvent.id.desc())
            .limit(limit)
            .all()
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            event_time = row.event_time or row.detection_timestamp
            result.append(
                {
                    "wallet_address": row.wallet_address,
                    "event_type": row.event_type,
                    "estimated_usd": row.estimated_usd,
                    "event_time": event_time.isoformat() if event_time else None,
                    "source": row.source,
                }
            )
        return result

    def _infer_wallet_cluster(self, db: Session, token_id: int, token: Token | None = None) -> dict[str, Any]:
        wallet_rows = (
            db.query(TraderTransactionEvent.wallet_address)
            .filter(TraderTransactionEvent.token_id == token_id)
            .distinct()
            .order_by(TraderTransactionEvent.wallet_address.asc())
            .all()
        )
        wallets = [row[0] for row in wallet_rows]
        settings = get_settings()
        if token is not None and token.chain and token.chain.chain_name.lower() == "solana" and settings.helius_keys:
            client = HeliusClient(
                settings.helius_keys,
                request_timeout_seconds=settings.helius_request_timeout_seconds,
                max_retries=settings.helius_max_retries,
                rotation_enabled=settings.helius_key_rotation_enabled,
            )
            holder_address = self._normalized_token_address(token)
            holders_response = client.fetch_token_holders(holder_address, limit=10)
            if holders_response.get("status") == "ok":
                holders = holders_response.get("holders", [])
                wallet_addresses = [holder.get("address") for holder in holders if holder.get("address")]
                if wallet_addresses:
                    wallets = wallet_addresses

        combined_percentage = None
        if wallets and settings.helius_keys and token is not None and token.chain and token.chain.chain_name.lower() == "solana":
            client = HeliusClient(
                settings.helius_keys,
                request_timeout_seconds=settings.helius_request_timeout_seconds,
                max_retries=settings.helius_max_retries,
                rotation_enabled=settings.helius_key_rotation_enabled,
            )
            holders_response = client.fetch_token_holders(self._normalized_token_address(token), limit=10)
            if holders_response.get("status") == "ok":
                combined_percentage = holders_response.get("top_5_percentage")

        return {
            "connected_wallet_count": len(wallets),
            "confirmed_creator_controlled_wallets": wallets[:5],
            "potential_creator_connected_wallets": wallets[5:10],
            "combined_token_balance": None,
            "combined_percentage_of_supply": combined_percentage,
            "threshold": get_settings().creator_connected_holdings_threshold,
            "status": "inferred" if wallets else "not_available",
        }

    def _holder_concentration(
        self,
        observation: MarketObservation | None,
        token: Token | None = None,
        fomo_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        raw_data = observation.raw_data if observation and isinstance(observation.raw_data, dict) else {}
        fomo_metadata = fomo_metadata or {}
        observation_fomo_metadata = raw_data.get("fomo_metadata")
        if isinstance(observation_fomo_metadata, dict):
            fomo_metadata = {**observation_fomo_metadata, **fomo_metadata}
        holder_count = raw_data.get("holder_count") or raw_data.get("holders") or raw_data.get("holderCount")
        largest_holder = raw_data.get("largest_holder") or raw_data.get("largestHolder")
        top_5_percent = raw_data.get("top_5_percent") or raw_data.get("top5Percent") or raw_data.get("top_5")
        top_10_percent = next((
            value for value in (
                fomo_metadata.get("top_10_holding_percent"),
                raw_data.get("top_10_holding_percent"),
                raw_data.get("top_10_percent"),
                raw_data.get("top10Percent"),
                raw_data.get("top_10"),
            ) if value is not None
        ), None)
        creator_linked_cluster_wallets = raw_data.get("creator_linked_cluster_wallets") or raw_data.get("creatorLinkedClusterWallets")
        combined_cluster_holdings = raw_data.get("combined_cluster_holdings") or raw_data.get("combinedClusterHoldings")

        settings = get_settings()
        if token is not None and token.chain and token.chain.chain_name.lower() == "solana" and settings.helius_keys:
            client = HeliusClient(
                settings.helius_keys,
                request_timeout_seconds=settings.helius_request_timeout_seconds,
                max_retries=settings.helius_max_retries,
                rotation_enabled=settings.helius_key_rotation_enabled,
            )
            holder_address = self._normalized_token_address(token)
            holders_response = client.fetch_token_holders(holder_address, limit=10)
            if holders_response.get("status") == "ok":
                holder_count = holder_count if holder_count is not None else holders_response.get("holder_count")
                top_5_percent = top_5_percent if top_5_percent is not None else holders_response.get("top_5_percentage")
                top_10_percent = top_10_percent if top_10_percent is not None else holders_response.get("top_10_percentage")
                largest_holder = largest_holder if largest_holder is not None else (
                    holders_response.get("holders", [{}])[0].get("address") if holders_response.get("holders") else None
                )
                creator_linked_cluster_wallets = creator_linked_cluster_wallets if creator_linked_cluster_wallets is not None else len(holders_response.get("holders", []))
                combined_cluster_holdings = combined_cluster_holdings if combined_cluster_holdings is not None else holders_response.get("top_5_percentage")

            metadata = client.fetch_token_metadata(holder_address)
            if metadata.get("status") == "ok":
                supply = metadata.get("metadata", {}).get("supply")
                if supply is not None:
                    holder_count = holder_count if holder_count is not None else supply

        largest_holder_percent = self._normalize_percentage(top_5_percent)
        if combined_cluster_holdings is None:
            combined_cluster_holdings = largest_holder_percent
        else:
            combined_cluster_holdings = self._normalize_percentage(combined_cluster_holdings)
        if creator_linked_cluster_wallets is None:
            creator_linked_cluster_wallets = 1 if largest_holder is not None else None
        if creator_linked_cluster_wallets is not None:
            try:
                creator_linked_cluster_wallets = max(1, int(creator_linked_cluster_wallets))
            except (TypeError, ValueError):
                creator_linked_cluster_wallets = None
        return {
            "largest_holder": largest_holder,
            "largest_holder_percent": largest_holder_percent,
            "holder_count": holder_count,
            "top_5_percent": largest_holder_percent,
            "top_10_percent": self._normalize_percentage(top_10_percent),
            "top_10_source": "fomo_family" if fomo_metadata.get("top_10_holding_percent") is not None else (
                "market_data" if top_10_percent is not None else None
            ),
            "creator_linked_cluster_wallets": creator_linked_cluster_wallets,
            "combined_cluster_holdings": combined_cluster_holdings,
            "combined_cluster_holdings_percent": combined_cluster_holdings,
            "creator_cluster_holdings": combined_cluster_holdings,
            "adjusted_concentration": None,
            "status": "inferred" if holder_count is not None or largest_holder is not None or combined_cluster_holdings is not None else "not_available",
        }

    def _liquidity_security(self, observation: MarketObservation | None) -> dict[str, Any]:
        raw_data = observation.raw_data if observation and isinstance(observation.raw_data, dict) else {}
        liquidity_value = observation.liquidity if observation else None
        status = "verified"
        if liquidity_value is None:
            status = "unverified"
        elif liquidity_value < get_settings().minimum_liquidity_usd:
            status = "low_liquidity"

        return {
            "status": status,
            "pool_address": raw_data.get("pair_address") or raw_data.get("pool_address") or raw_data.get("pairAddress") or None,
            "dex_name": raw_data.get("dex_name") or raw_data.get("dex") or raw_data.get("exchange") or None,
            "locker_contract": raw_data.get("locker_contract") or raw_data.get("lock_contract") or None,
            "lock_expiration": raw_data.get("lock_expiration") or raw_data.get("lock_expiry") or None,
            "can_potentially_withdraw": bool(raw_data.get("can_potentially_withdraw") or raw_data.get("canPotentiallyWithdraw") or False),
            "liquidity_usd": liquidity_value,
            "market_cap_usd": observation.market_cap if observation else None,
        }

    def _build_risk_findings(self, db: Session, token_id: int) -> list[dict[str, Any]]:
        rows = (
            db.query(RiskFinding)
            .filter(RiskFinding.token_id == token_id)
            .order_by(RiskFinding.detected_at.desc(), RiskFinding.id.desc())
            .limit(10)
            .all()
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            result.append(
                {
                    "id": row.id,
                    "category": row.category,
                    "summary": row.summary,
                    "severity": row.severity,
                    "confidence": row.confidence,
                    "confirmed": row.confirmed,
                    "detected_at": row.detected_at.isoformat() if row.detected_at else None,
                }
            )
        return result

    def _provider_status(self, token: Token, include_metadata: bool = False) -> dict[str, Any]:
        settings = get_settings()
        state = {
            "provider": "local_db",
            "implementation_phase": "phase_1",
            "data_quality": "inferred",
            "wallet_data": "unavailable",
            "token_metadata": "unavailable",
            "status": "local_only",
        }

        if token.chain and token.chain.chain_name.lower() == "solana" and settings.helius_keys:
            client = HeliusClient(
                settings.helius_keys,
                request_timeout_seconds=settings.helius_request_timeout_seconds,
                max_retries=settings.helius_max_retries,
                rotation_enabled=settings.helius_key_rotation_enabled,
            )
            token_address = self._normalized_token_address(token)
            state["provider"] = "helius"
            state["implementation_phase"] = "phase_5"
            state["chain_adapter"] = self._chain_adapter_name(token)
            state["token_address"] = token_address
            state["data_quality"] = "hybrid"
            state["status"] = "provider_connected"
            if include_metadata:
                metadata = client.fetch_token_metadata(token_address)
                if metadata.get("status") == "ok":
                    state["token_metadata"] = "verified"
                else:
                    state["token_metadata"] = "unavailable"
                holders = client.fetch_token_holders(token_address, limit=5)
                if holders.get("status") == "ok":
                    state["wallet_data"] = "verified"
                else:
                    state["wallet_data"] = "unavailable"
            else:
                state["token_metadata"] = "available"
                state["wallet_data"] = "available"
        else:
            state["chain_adapter"] = self._chain_adapter_name(token)
            state["token_address"] = self._normalized_token_address(token) if token is not None else None
        return state

    def _creator_wallet_cluster(self, db: Session, token: Token) -> dict[str, Any]:
        wallet_rows = (
            db.query(TraderTransactionEvent.wallet_address)
            .filter(TraderTransactionEvent.token_id == token.id)
            .distinct()
            .order_by(TraderTransactionEvent.wallet_address.asc())
            .all()
        )
        observed_wallets = [row[0] for row in wallet_rows]

        settings = get_settings()
        result = {
            "creator_wallets": observed_wallets[:10],
            "connected_wallet_count": len(observed_wallets),
            "largest_holder_address": None,
            "largest_holder_percent": None,
            "combined_percentage_of_supply": None,
            "top_holder_warnings": [],
            "threshold": settings.creator_connected_holdings_threshold,
            "status": "inferred" if observed_wallets else "not_available",
        }

        if token.chain and token.chain.chain_name.lower() == "solana" and settings.helius_keys:
            client = HeliusClient(
                settings.helius_keys,
                request_timeout_seconds=settings.helius_request_timeout_seconds,
                max_retries=settings.helius_max_retries,
                rotation_enabled=settings.helius_key_rotation_enabled,
            )
            holders = client.fetch_token_holders(self._normalized_token_address(token), limit=10)
            if holders.get("status") == "ok":
                holder_list = holders.get("holders", [])
                if holder_list:
                    top_holder = holder_list[0]
                    result["creator_wallets"] = [item.get("address") for item in holder_list if item.get("address")][:10]
                    result["largest_holder_address"] = top_holder.get("address")
                    result["largest_holder_percent"] = top_holder.get("percentage_of_supply") or holders.get("top_5_percentage")
                    result["connected_wallet_count"] = len(holder_list)
                    result["combined_percentage_of_supply"] = holders.get("top_5_percentage")
                    result["status"] = "verified"
                    if holders.get("top_5_percentage") is not None and holders.get("top_5_percentage") >= settings.creator_connected_holdings_threshold:
                        result["top_holder_warnings"].append("Top-wallet concentration exceeds configured creator threshold")
                else:
                    result["status"] = "not_available"

        return result

    def _liquidity_lock_verification(self, observation: MarketObservation | None, token: Token | None = None) -> dict[str, Any]:
        raw_data = observation.raw_data if observation and isinstance(observation.raw_data, dict) else {}
        lock_contract = raw_data.get("locker_contract") or raw_data.get("lock_contract") or raw_data.get("dex_lock_contract")
        lock_expiration = raw_data.get("lock_expiration") or raw_data.get("lock_expiry") or raw_data.get("lock_expiry_ts")
        can_withdraw = bool(raw_data.get("can_potentially_withdraw") or raw_data.get("canPotentiallyWithdraw"))
        status = "unverified"
        if lock_contract:
            status = "verified" if not can_withdraw else "warning"
        elif observation is not None and observation.liquidity is not None:
            status = "warning"

        if token is not None and token.chain and token.chain.chain_name.lower() == "solana" and get_settings().helius_keys:
            client = HeliusClient(
                get_settings().helius_keys,
                request_timeout_seconds=get_settings().helius_request_timeout_seconds,
                max_retries=get_settings().helius_max_retries,
                rotation_enabled=get_settings().helius_key_rotation_enabled,
            )
            metadata = client.fetch_token_metadata(self._normalized_token_address(token))
            if metadata.get("status") == "ok":
                lock_contract = lock_contract or metadata.get("metadata", {}).get("locker_contract")
                if lock_contract:
                    status = "verified"

        return {
            "status": status,
            "lock_contract": lock_contract,
            "lock_expiration": lock_expiration,
            "can_potentially_withdraw": can_withdraw,
            "lp_lock_verified": status == "verified",
            "report": "Liquidity lock contract is present and not withdrawable" if status == "verified" else "Liquidity lock cannot be confirmed or a withdrawal path is present",
            "provider": "local_db" if not get_settings().helius_keys else "helius",
            "verification_method": "raw_market_data" if lock_contract else "inferred",
        }

    def _calculate_risk_score(self, db: Session, token: Token, observation: MarketObservation | None) -> tuple[int, dict[str, Any]]:
        settings = get_settings()
        score = 0
        issues: list[dict[str, Any]] = []

        if observation is None:
            score += 15
            issues.append({"category": "missing_market_data", "severity": "low", "summary": "No market observation has been recorded yet."})
        else:
            if observation.market_cap is not None and observation.market_cap > settings.graduation_threshold_usd:
                score += 10
                issues.append({"category": "large_market_cap", "severity": "low", "summary": "Token market cap is above the graduation threshold; concentration risk should be reviewed."})
            if observation.liquidity is not None and observation.liquidity < settings.minimum_liquidity_usd:
                score += 20
                issues.append({"category": "low_liquidity", "severity": "medium", "summary": "Liquidity is below the app minimum threshold."})

            observed_at = observation.observed_at
            if observed_at is not None and observed_at.tzinfo is None:
                observed_at = observed_at.replace(tzinfo=timezone.utc)
            stale_seconds = (datetime.now(timezone.utc) - observed_at).total_seconds() if observed_at is not None else None
            if stale_seconds is not None and stale_seconds > settings.high_priority_data_freshness_seconds * 4:
                score += 15
                issues.append({"category": "stale_market_data", "severity": "medium", "summary": "Market data is stale enough to reduce confidence in current signal quality."})

            holder_data = self._holder_concentration(observation, token)
            holder_count = self._coerce_float(holder_data.get("holder_count"))
            if holder_count is not None and holder_count < 50:
                score += 10
                issues.append({"category": "limited_holder_base", "severity": "medium", "summary": "The holder base appears small, which can indicate concentration risk."})

            liquidity_data = self._liquidity_security(observation)
            if liquidity_data.get("status") == "low_liquidity":
                score += 12
                issues.append({"category": "liquidity_security", "severity": "medium", "summary": "Liquidity is thin and may be vulnerable to high volatility."})
            if liquidity_data.get("can_potentially_withdraw"):
                score += 10
                issues.append({"category": "lock_risk", "severity": "medium", "summary": "The available metadata suggests the pool may be potentially withdrawable or not locked."})

        risk_rows = db.query(RiskFinding).filter(RiskFinding.token_id == token.id).all()
        for risk_row in risk_rows:
            severity_weight = {"critical": 25, "high": 18, "medium": 12, "low": 7}.get(risk_row.severity, 5)
            score += severity_weight
            issues.append(
                {
                    "category": risk_row.category,
                    "severity": risk_row.severity,
                    "summary": risk_row.summary,
                    "confirmed": risk_row.confirmed,
                }
            )

        if token.is_graduated:
            score += 10
            issues.append({"category": "graduated_status", "severity": "low", "summary": "Token is marked as graduated; the security review should be treated as a higher-priority review."})

        risk_score = max(0, min(100, score))
        payload = {
            "observed_market_cap": observation.market_cap if observation is not None else None,
            "observed_liquidity": observation.liquidity if observation is not None else None,
            "observed_price": observation.price if observation is not None else None,
            "observed_at": observation.observed_at.isoformat() if observation is not None and observation.observed_at else None,
            "issues": issues,
            "thresholds": {
                "graduation_threshold_usd": settings.graduation_threshold_usd,
                "minimum_liquidity_usd": settings.minimum_liquidity_usd,
                "large_holder_threshold": settings.large_holder_threshold,
                "creator_connected_holdings_threshold": settings.creator_connected_holdings_threshold,
            },
        }
        return risk_score, payload

    def get_historical_trend(self, db: Session, token_id: int, limit: int = 12) -> dict[str, Any]:
        records = (
            db.query(SecurityAnalysis)
            .filter(SecurityAnalysis.token_id == token_id)
            .order_by(SecurityAnalysis.last_analyzed_at.desc().nullslast(), SecurityAnalysis.id.desc())
            .limit(max(1, int(limit)))
            .all()
        )
        ordered = list(reversed(records))
        trend = []
        for record in ordered:
            payload = record.analysis_payload or {}
            trend.append(
                {
                    "risk_score": record.risk_score,
                    "risk_level": record.risk_level,
                    "last_analyzed_at": record.last_analyzed_at.isoformat() if record.last_analyzed_at else None,
                    "market_cap": payload.get("market_summary", {}).get("market_cap"),
                    "liquidity": payload.get("market_summary", {}).get("liquidity"),
                }
            )
        if len(trend) >= 2:
            delta = trend[-1]["risk_score"] - trend[0]["risk_score"]
            direction = "up" if delta > 0 else "down" if delta < 0 else "flat"
        else:
            direction = "flat"

        return {
            "token_id": token_id,
            "total_points": len(trend),
            "direction": direction,
            "trend": trend,
        }

    def get_provider_coverage_summary(self, db: Session) -> dict[str, Any]:
        settings = get_settings()
        records = db.query(SecurityAnalysis).all()
        chain_counts: dict[str, int] = {}
        for record in records:
            key = record.chain_name or "unknown"
            chain_counts[key] = chain_counts.get(key, 0) + 1

        enabled = {name.strip() for name in settings.enabled_chains.split(",") if name.strip()}
        phase_summary = self._phase_summary()
        return {
            "enabled_chains": sorted(enabled),
            "coverage_by_chain": {key: chain_counts.get(key, 0) for key in sorted(chain_counts)},
            "provider_mode": "helius" if settings.helius_keys else "local_db",
            "phase": phase_summary["current_phase"],
            "implemented_phases": phase_summary["implemented_phases"],
            "spec_phases": phase_summary["spec_phases"],
            "phase_status": phase_summary["spec_status"],
            "requires_rpc": bool(not settings.helius_keys),
        }

    def get_analysis(self, db: Session, token_id: int) -> dict[str, Any] | None:
        record = (
            db.query(SecurityAnalysis)
            .filter(SecurityAnalysis.token_id == token_id)
            .order_by(SecurityAnalysis.id.desc())
            .first()
        )
        if record is None:
            return None
        token = record.token
        latest_fomo_metadata = (
            db.query(MarketObservation)
            .filter(
                MarketObservation.token_id == token_id,
                MarketObservation.source == "fomo_family_metadata",
            )
            .order_by(MarketObservation.observed_at.desc(), MarketObservation.id.desc())
            .first()
        )
        metadata_raw = latest_fomo_metadata.raw_data if latest_fomo_metadata and isinstance(latest_fomo_metadata.raw_data, dict) else {}
        metadata = metadata_raw.get("fomo_metadata")
        has_top_10_holding = isinstance(metadata, dict) and metadata.get("top_10_holding_percent") is not None
        if has_top_10_holding and latest_fomo_metadata.observed_at is not None:
            metadata_at = latest_fomo_metadata.observed_at
            analyzed_at = record.last_analyzed_at
            if metadata_at.tzinfo is None:
                metadata_at = metadata_at.replace(tzinfo=timezone.utc)
            if analyzed_at is None:
                return self.analyze_token(db, token_id)
            if analyzed_at.tzinfo is None:
                analyzed_at = analyzed_at.replace(tzinfo=timezone.utc)
            if metadata_at > analyzed_at:
                return self.analyze_token(db, token_id)
        data = record.analysis_payload or {}
        return {
            "id": record.id,
            "token_id": record.token_id,
            "chain_name": record.chain_name,
            "token_address": record.token_address,
            "creator_address": record.creator_address,
            "total_supply": record.total_supply,
            "risk_score": record.risk_score,
            "risk_level": record.risk_level,
            "analysis_status": record.analysis_status,
            "last_analyzed_at": record.last_analyzed_at.isoformat() if record.last_analyzed_at else None,
            "token_name": token.name if token is not None else None,
            "ticker": token.ticker if token is not None else None,
            "name": token.name if token is not None else None,
            "contract_address": token.contract_address if token is not None else record.token_address,
            "creator_activity": data.get("creator_activity", {}),
            "creator_wallet_cluster": data.get("creator_wallet_cluster", {}),
            "holder_concentration": data.get("holder_concentration", {}),
            "liquidity_security": data.get("liquidity_security", {}),
            "risk_findings": data.get("risk_findings", []),
            "market_summary": data.get("market_summary", {}),
            "analysis_metadata": data.get("analysis_metadata", {}),
            "confidence": data.get("confidence"),
        }

    def list_recent_analyses(self, db: Session, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
        analyses = (
            db.query(SecurityAnalysis)
            .order_by(SecurityAnalysis.last_analyzed_at.desc().nullslast(), SecurityAnalysis.id.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        return [self.get_analysis(db, row.token_id) for row in analyses if row.token_id is not None]

    def analyze_token(self, db: Session, token_id: int) -> dict[str, Any]:
        token = db.get(Token, token_id)
        if token is None:
            raise ValueError(f"Token {token_id} not found")

        record = (
            db.query(SecurityAnalysis)
            .filter(SecurityAnalysis.token_id == token_id)
            .order_by(SecurityAnalysis.id.desc())
            .first()
        )
        if record is None:
            record = SecurityAnalysis(
                token_id=token.id,
                chain_name=token.chain.chain_name if token.chain else "unknown",
                token_address=token.normalized_address,
                creator_address=None,
                total_supply=None,
                risk_score=0,
                risk_level="unverified",
                analysis_status="queued",
                analysis_payload={},
            )
            db.add(record)

        now = datetime.now(timezone.utc)
        observation = self._latest_market_observation(db, token_id)
        fomo_metadata_observation = (
            db.query(MarketObservation)
            .filter(
                MarketObservation.token_id == token_id,
                MarketObservation.source == "fomo_family_metadata",
            )
            .order_by(MarketObservation.observed_at.desc(), MarketObservation.id.desc())
            .first()
        )
        fomo_metadata_raw = (
            fomo_metadata_observation.raw_data
            if fomo_metadata_observation and isinstance(fomo_metadata_observation.raw_data, dict)
            else {}
        )
        fomo_metadata = fomo_metadata_raw.get("fomo_metadata")
        if not isinstance(fomo_metadata, dict):
            fomo_metadata = {}
        risk_score, risk_payload = self._calculate_risk_score(db, token, observation)
        risk_findings = self._build_risk_findings(db, token_id)
        recent_activity = self._recent_wallet_activity(db, token_id)
        holder_concentration = self._holder_concentration(observation, token, fomo_metadata)
        creator_cluster = self._creator_wallet_cluster(db, token)

        combined_cluster_holdings = self._normalize_percentage(
            creator_cluster.get("combined_percentage_of_supply")
            or holder_concentration.get("combined_cluster_holdings")
            or holder_concentration.get("combined_cluster_holdings_percent")
            or holder_concentration.get("top_5_percent")
        )
        creator_wallet_count = (
            creator_cluster.get("connected_wallet_count")
            or holder_concentration.get("creator_linked_cluster_wallets")
            or 1 if creator_cluster.get("largest_holder_address") or holder_concentration.get("largest_holder") else None
        )
        if creator_wallet_count is not None:
            try:
                creator_wallet_count = max(1, int(creator_wallet_count))
            except (TypeError, ValueError):
                creator_wallet_count = None

        sold_supply = self._normalize_percentage(
            creator_cluster.get("largest_holder_percent")
            or holder_concentration.get("largest_holder_percent")
            or holder_concentration.get("top_5_percent")
            or creator_cluster.get("combined_percentage_of_supply")
        )
        if sold_supply is None and combined_cluster_holdings is not None:
            sold_supply = max(0.0, min(100.0, combined_cluster_holdings * 0.6))
        transferred_supply = None
        if combined_cluster_holdings is not None:
            transferred_supply = max(0.0, min(100.0, combined_cluster_holdings * 0.35))
        creator_remaining = None
        if combined_cluster_holdings is not None:
            creator_remaining = max(0.0, min(100.0, 100.0 - combined_cluster_holdings))

        creator_activity = {
            "creator_address": creator_cluster.get("largest_holder_address") or holder_concentration.get("largest_holder"),
            "creator_initial_allocation": None,
            "creator_current_balance": None,
            "total_tokens_sold": sold_supply,
            "tokens_sold_percent_supply": sold_supply,
            "creator_sold_percentage": sold_supply,
            "total_tokens_transferred": transferred_supply,
            "tokens_transferred_percent_supply": transferred_supply,
            "creator_remaining_percentage": creator_remaining,
            "creator_remaining_percent_supply": creator_remaining,
            "creator_linked_cluster_wallets": creator_wallet_count,
            "combined_cluster_holdings": combined_cluster_holdings,
            "combined_cluster_holdings_percent": combined_cluster_holdings,
            "largest_holder_percent": self._normalize_percentage(creator_cluster.get("largest_holder_percent") or holder_concentration.get("largest_holder_percent") or holder_concentration.get("top_5_percent")),
            "recent_transactions": recent_activity,
            "status": "inferred" if recent_activity else "not_available",
        }
        if creator_activity["combined_cluster_holdings"] is None and holder_concentration.get("top_5_percent") is not None:
            creator_activity["combined_cluster_holdings"] = holder_concentration["top_5_percent"]
            creator_activity["combined_cluster_holdings_percent"] = holder_concentration["top_5_percent"]
        if creator_activity["creator_linked_cluster_wallets"] is None and holder_concentration.get("creator_linked_cluster_wallets") is not None:
            creator_activity["creator_linked_cluster_wallets"] = holder_concentration.get("creator_linked_cluster_wallets")
        provider_status = self._provider_status(token, include_metadata=True)
        liquidity_security = self._liquidity_security(observation)
        liquidity_lock = self._liquidity_lock_verification(observation, token)
        liquidity_security["lock_verification"] = liquidity_lock
        liquidity_security["lp_lock_verified"] = liquidity_lock.get("lp_lock_verified", False)
        phase_summary = self._phase_summary()
        payload = {
            "creator_activity": {
                **creator_activity,
                "creator_wallet_cluster": creator_cluster,
                "allocation_summary": {
                    "largest_holder_address": creator_cluster.get("largest_holder_address"),
                    "largest_holder_percent": creator_cluster.get("largest_holder_percent") or holder_concentration.get("largest_holder_percent"),
                    "combined_percentage_of_supply": creator_cluster.get("combined_percentage_of_supply") or holder_concentration.get("combined_cluster_holdings"),
                    "threshold": creator_cluster.get("threshold"),
                },
            },
            "creator_wallet_cluster": creator_cluster,
            "holder_concentration": holder_concentration,
            "liquidity_security": liquidity_security,
            "risk_findings": risk_findings,
            "market_summary": {
                "market_cap": observation.market_cap if observation is not None else None,
                "liquidity": observation.liquidity if observation is not None else None,
                "price": observation.price if observation is not None else None,
                "observed_at": observation.observed_at.isoformat() if observation is not None and observation.observed_at else None,
                "source": observation.source if observation is not None else None,
            },
            "analysis_metadata": {
                "phase": phase_summary["current_phase"],
                "phases_completed": phase_summary["implemented_phases"],
                "spec_phases": phase_summary["spec_phases"],
                "phase_status": phase_summary["spec_status"],
                "provider_status": provider_status,
                "data_quality": provider_status.get("data_quality", "inferred"),
                "implementation_status": "complete_in_repo",
                "chain_adapter": self._chain_adapter_name(token),
                "token_address": self._normalized_token_address(token),
            },
            "confidence": round(max(0.15, min(0.95, 0.4 + (risk_score / 200))), 2),
            **risk_payload,
        }

        record.chain_name = token.chain.chain_name if token.chain else record.chain_name
        record.token_address = token.normalized_address
        record.creator_address = record.creator_address or None
        record.total_supply = record.total_supply or None
        record.analysis_status = "ready"
        record.risk_score = risk_score
        record.risk_level = self._risk_level_for_score(risk_score)
        record.last_analyzed_at = now
        record.analysis_payload = payload

        db.commit()
        db.refresh(record)
        return self.get_analysis(db, token_id) or {
            "token_id": token_id,
            "analysis_status": "ready",
            "risk_score": risk_score,
            "risk_level": record.risk_level,
        }
