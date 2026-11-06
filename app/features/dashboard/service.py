from __future__ import annotations

from sqlalchemy.orm import Session

from app.features.graduated_tokens.service import count_graduated_tokens
from app.features.tokens.service import active_token_count
from app.models import (
    DismissedToken,
    DiscoveryEvent,
    Favorite,
    FollowedTrader,
    RiskFinding,
    SecurityAnalysis,
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
        "graduations": count_graduated_tokens(db),
        "risk_findings": db.query(RiskFinding).count(),
        "security_analyses": db.query(SecurityAnalysis).count(),
        "dismissed_tokens": db.query(DismissedToken).count(),
        "status": "ok",
        "last_successful_refresh": "not yet run",
    }
