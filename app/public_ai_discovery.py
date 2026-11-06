from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal, get_args

import httpx
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, ValidationError

PAGE_DIR = Path(__file__).resolve().parent.parent / "frontend" / "ai-discovery"
ASSETS_DIR = PAGE_DIR / "assets"
TRACKER_API_URL = "http://127.0.0.1:8000/api/v1/ai-discovery/"

DiscoveryStatus = Literal[
    "disabled",
    "scan_pending",
    "scanning",
    "model_pending",
    "failed",
    "no_tracked_tokens",
    "ready",
    "fallback_ready",
    "model_assisted",
    "signal_only",
    "heuristic_fallback",
    "unavailable",
]


class PublicCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rank: Annotated[int, Field(ge=1)]
    name: str | None = None
    ticker: str | None = None
    chain_name: str | None = None
    contract_address: str | None = None
    fomo_url: str | None = None
    image_url: str | None = None
    ai_discovery_since: datetime | None = None
    is_favorite: bool = False
    score: FiniteFloat | None = None
    risk_score: FiniteFloat | None = None
    data_completeness_percent: FiniteFloat | None = None
    market_cap: FiniteFloat | None = None
    ai_rmc: FiniteFloat | None = None
    ai_rh: FiniteFloat | None = None
    liquidity: FiniteFloat | None = None
    buyer_count: Annotated[int, Field(ge=0)] | None = None
    fomo_holder_count: Annotated[int, Field(ge=0)] | None = None


class PublicAIStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: DiscoveryStatus
    tokens_scanned: Annotated[int, Field(ge=0)]
    candidate_count: Annotated[int, Field(ge=0)]
    last_run_at: datetime | None
    candidates: list[PublicCandidate]


PUBLIC_CANDIDATE_FIELDS = tuple(PublicCandidate.model_fields)

app = FastAPI(
    title="Public AI Discovery",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


def _project_status(payload: object) -> PublicAIStatus:
    if not isinstance(payload, dict) or not isinstance(payload.get("candidates"), list):
        raise HTTPException(status_code=502, detail="The local tracker returned invalid AI Discovery data.")

    try:
        candidates: list[PublicCandidate] = []
        for candidate in payload["candidates"]:
            if not isinstance(candidate, dict):
                raise ValueError("Invalid candidate row.")
            allowed_fields = {
                field: candidate[field]
                for field in PUBLIC_CANDIDATE_FIELDS
                if field in candidate
            }
            candidates.append(PublicCandidate.model_validate(allowed_fields))

        status_value = payload.get("status")
        allowed_statuses = get_args(DiscoveryStatus)
        status: DiscoveryStatus = (
            status_value
            if isinstance(status_value, str) and status_value in allowed_statuses
            else "unavailable"
        )
        return PublicAIStatus.model_validate({
            "status": status,
            "tokens_scanned": payload.get("tokens_scanned", 0),
            "candidate_count": len(candidates),
            "last_run_at": payload.get("last_run_at"),
            "candidates": candidates,
        })
    except (TypeError, ValueError, ValidationError) as error:
        raise HTTPException(status_code=502, detail="The local tracker returned invalid AI Discovery data.") from error


@app.get("/api/ai-discovery", response_model=PublicAIStatus)
async def public_ai_discovery(response: Response) -> PublicAIStatus:
    response.headers["Cache-Control"] = "no-store"
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
            tracker_response = await client.get(
                TRACKER_API_URL,
                headers={"Accept": "application/json"},
            )
            tracker_response.raise_for_status()
            payload = tracker_response.json()
    except httpx.TimeoutException as error:
        raise HTTPException(status_code=503, detail="The local tracker API request timed out.") from error
    except httpx.HTTPStatusError as error:
        raise HTTPException(status_code=503, detail="The local tracker API returned an error.") from error
    except (httpx.RequestError, ValueError) as error:
        raise HTTPException(status_code=503, detail="The local tracker API is unavailable.") from error

    return _project_status(payload)


@app.get("/ai-discovery/", include_in_schema=False)
async def public_ai_discovery_page() -> FileResponse:
    return FileResponse(
        PAGE_DIR / "index.html",
        media_type="text/html",
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' https: data:; connect-src 'self'; base-uri 'none'; "
                "form-action 'none'; frame-ancestors 'none'"
            ),
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
        },
    )


app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="public-ai-discovery-assets")
