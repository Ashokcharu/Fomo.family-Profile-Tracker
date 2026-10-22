from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.features.tokens.service import filter_token_records, token_display_data
from app.models import DismissedToken, RiskFinding, Token


def list_dismissed_tokens(db: Session, limit: int = 20, offset: int = 0, search: str | None = None) -> list[dict]:
    query = filter_token_records(db.query(DismissedToken), DismissedToken.token_id, search)
    rows = query.order_by(DismissedToken.dismissed_at.desc()).offset(offset).limit(limit).all()
    token_data = token_display_data(db, [row.token_id for row in rows])
    return [
        {
            "id": row.id,
            "token_id": row.token_id,
            "dismissed_at": row.dismissed_at.isoformat(),
            "reason": row.reason,
            "restored": row.restored,
            **token_data.get(row.token_id, {}),
        }
        for row in rows
    ]


def count_dismissed_tokens(db: Session, search: str | None = None) -> int:
    query = filter_token_records(db.query(func.count(DismissedToken.id)), DismissedToken.token_id, search)
    return query.scalar() or 0


def delete_dismissed_tokens(db: Session, dismissed_ids: list[int]) -> int:
    unique_ids = set(dismissed_ids)
    if not unique_ids:
        return 0
    deleted = db.query(DismissedToken).filter(DismissedToken.id.in_(unique_ids)).delete(synchronize_session=False)
    db.commit()
    return deleted


def active_dismissed_token_ids(db: Session) -> set[int]:
    latest_by_token: dict[int, bool] = {}
    for token_id, restored in (
        db.query(DismissedToken.token_id, DismissedToken.restored)
        .order_by(DismissedToken.dismissed_at.desc(), DismissedToken.id.desc())
        .all()
    ):
        latest_by_token.setdefault(token_id, restored)
    return {token_id for token_id, restored in latest_by_token.items() if not restored}


def dismiss_tokens(db: Session, token_ids: list[int], reason: str | None = None) -> dict[str, int]:
    unique_ids = set(token_ids)
    existing_ids = {
        token_id for (token_id,) in db.query(Token.id).filter(Token.id.in_(unique_ids)).all()
    } if unique_ids else set()
    if not existing_ids:
        return {"dismissed": 0, "risk_findings_deleted": 0}

    already_dismissed = active_dismissed_token_ids(db)
    new_dismissals = existing_ids - already_dismissed
    for token_id in new_dismissals:
        db.add(DismissedToken(token_id=token_id, reason=reason, restored=False))
    findings_deleted = db.query(RiskFinding).filter(RiskFinding.token_id.in_(existing_ids)).delete(
        synchronize_session=False
    )
    db.commit()
    return {"dismissed": len(new_dismissals), "risk_findings_deleted": findings_deleted}


def dismiss_token(db: Session, token: Token, reason: str | None = None) -> DismissedToken:
    event = DismissedToken(token_id=token.id, reason=reason, restored=False)
    db.add(event)
    db.commit()
    db.refresh(event)
    return event
