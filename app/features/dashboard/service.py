from __future__ import annotations

from sqlalchemy.orm import Session

from app.features.tokens.service import active_token_count
from app.models import (
    DismissedToken,
    DiscoveryEvent,
    Favorite,
    FollowedTrader,
    GraduationEvent,
    RiskFinding,
    Token,
    TraderTransactionEvent,
)


def get_overview(db: Session) -> dict:
    return {
        "tokens": active_token_count(db),
        "total_tokens": db.query(Token).count(),
        "discoveries": db.query(DiscoveryEvent).count(),
        "followed_traders": db.query(FollowedTrader).count(),
        "buys": db.query(TraderTransactionEvent).filter(
            TraderTransactionEvent.source == "fomo_family",
            TraderTransactionEvent.event_type == "buy",
        ).count(),
        "favorites": db.query(Favorite).count(),
        "graduations": db.query(GraduationEvent).count(),
        "risk_findings": db.query(RiskFinding).count(),
        "dismissed_tokens": db.query(DismissedToken).count(),
        "status": "ok",
        "last_successful_refresh": "not yet run",
    }
