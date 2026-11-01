from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.features.tokens.service import filter_token_records, token_display_data
from app.models import RiskFinding, Token


def list_risk_findings(db: Session, limit: int = 20, offset: int = 0, search: str | None = None) -> list[dict]:
    query = filter_token_records(db.query(RiskFinding), RiskFinding.token_id, search)
    rows = query.order_by(RiskFinding.detected_at.desc()).offset(offset).limit(limit).all()
    token_data = token_display_data(db, [row.token_id for row in rows])
    return [
        {
            "id": row.id,
            "token_id": row.token_id,
            "severity": row.severity,
            "category": row.category,
            "summary": row.summary,
            "confirmed": row.confirmed,
            "confidence": row.confidence,
            "detected_at": row.detected_at.isoformat(),
            **token_data.get(row.token_id, {}),
        }
        for row in rows
    ]


def count_risk_findings(db: Session, search: str | None = None) -> int:
    query = filter_token_records(db.query(func.count(RiskFinding.id)), RiskFinding.token_id, search)
    return query.scalar() or 0


def delete_risk_findings(db: Session, finding_ids: list[int]) -> int:
    unique_ids = set(finding_ids)
    if not unique_ids:
        return 0
    deleted = db.query(RiskFinding).filter(RiskFinding.id.in_(unique_ids)).delete(synchronize_session=False)
    db.commit()
    return deleted


def add_risk_finding(db: Session, token: Token, category: str, summary: str, severity: str = "medium", confirmed: bool = False) -> RiskFinding:
    finding = RiskFinding(
        token_id=token.id,
        category=category,
        summary=summary,
        severity=severity,
        confirmed=confirmed,
        confidence=0.5,
    )
    db.add(finding)
    db.commit()
    db.refresh(finding)
    return finding


def record_rmc_drawdown_risk(
    db: Session,
    token: Token,
    reach_market_cap: float | None,
    current_market_cap: float | None,
) -> RiskFinding | None:
    if reach_market_cap is None or reach_market_cap <= 0 or current_market_cap is None:
        return None
    drawdown_percent = (1 - current_market_cap / reach_market_cap) * 100
    if drawdown_percent < 90:
        return None

    category = "rmc_drawdown"
    finding = (
        db.query(RiskFinding)
        .filter(RiskFinding.token_id == token.id, RiskFinding.category == category)
        .first()
    )
    summary = f"Market cap is down {drawdown_percent:.1f}% from RMC."
    evidence = f"RMC ${reach_market_cap:,.0f}; current market cap ${current_market_cap:,.0f}."
    if finding is None:
        finding = RiskFinding(
            token_id=token.id,
            severity="critical",
            category=category,
            summary=summary,
            evidence=evidence,
            source="market_refresh",
            confidence=1.0,
            risk_metadata={
                "reach_market_cap": reach_market_cap,
                "current_market_cap": current_market_cap,
                "drawdown_percent": drawdown_percent,
            },
        )
        db.add(finding)
    else:
        finding.summary = summary
        finding.evidence = evidence
        finding.risk_metadata = {
            "reach_market_cap": reach_market_cap,
            "current_market_cap": current_market_cap,
            "drawdown_percent": drawdown_percent,
        }
    return finding
