from __future__ import annotations

import re
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from app import public_ai_discovery

SAMPLE_CANDIDATE = {
    "rank": 1,
    "name": "Example",
    "ticker": "EX",
    "chain_name": "base",
    "contract_address": "0x123",
    "fomo_url": "https://fomo.family/tokens/base/0x123",
    "image_url": "https://images.example.com/example.png",
    "ai_discovery_since": "2026-11-05T00:00:00Z",
    "is_favorite": False,
    "score": 75,
    "risk_score": 20,
    "data_completeness_percent": 80,
    "market_cap": 50_000,
    "ai_rmc": 25_000,
    "ai_rh": 60_000,
    "liquidity": 10_000,
    "buyer_count": 7,
    "fomo_holder_count": 12,
    "token_id": 456,
    "details": "private analysis",
    "remove_endpoint": "/api/v1/ai-discovery/456",
    "wallet_credentials": "must never be public",
}

SAMPLE_STATUS = {
    "status": "model_assisted",
    "tokens_scanned": 25,
    "candidate_count": 1,
    "last_run_at": "2026-11-05T00:00:00Z",
    "candidates": [SAMPLE_CANDIDATE],
    "evaluation_report": {"not": "public"},
    "model": "private-model-config",
    "job_id": 987,
}


class MockResponse:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> object:
        return self.payload


def install_tracker_response(monkeypatch, payload: object, requests: list[tuple[str, str]]) -> None:
    class MockAsyncClient:
        def __init__(self, **kwargs) -> None:
            assert kwargs["follow_redirects"] is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            pass

        async def get(self, url: str, **kwargs) -> MockResponse:
            requests.append(("GET", url))
            assert kwargs["headers"]["Accept"] == "application/json"
            return MockResponse(payload)

    monkeypatch.setattr(public_ai_discovery.httpx, "AsyncClient", MockAsyncClient)


def test_public_page_loads_only_at_ai_discovery_path() -> None:
    client = TestClient(public_ai_discovery.app)
    page = client.get("/ai-discovery/")

    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "AI Discovery" in page.text
    assert re.findall(r'<th scope="col"[^>]*>(.*?)</th>', page.text) == [
        "Rank",
        "Token",
        "HH",
        "Fav.",
        "Signal",
        "Risk",
        "Data",
        "MC",
        "AI RMC",
        "AIRH",
        "AI RMC→MC",
        "AI RMC→AIRH",
        "Liq.",
        "FU",
        "FH",
    ]
    assert "Details" not in page.text
    assert "Remove" not in page.text
    assert page.headers["x-content-type-options"] == "nosniff"
    assert client.get("/").status_code == 404
    assert client.get("/assets/app.js").status_code == 200
    assert client.get("/assets/styles.css").status_code == 200
    assert client.get("/assets/README.md").status_code == 404


def test_public_api_returns_candidate_rows_with_only_allowlisted_fields(monkeypatch) -> None:
    requests: list[tuple[str, str]] = []
    install_tracker_response(monkeypatch, SAMPLE_STATUS, requests)

    response = TestClient(public_ai_discovery.app).get("/api/ai-discovery")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload["status"] == "model_assisted"
    assert payload["tokens_scanned"] == 25
    assert payload["candidate_count"] == 1
    assert payload["last_run_at"] == "2026-11-05T00:00:00Z"
    assert set(payload["candidates"][0]) == set(public_ai_discovery.PUBLIC_CANDIDATE_FIELDS)
    assert payload["candidates"][0] == {
        "rank": 1,
        "name": "Example",
        "ticker": "EX",
        "chain_name": "base",
        "contract_address": "0x123",
        "fomo_url": "https://fomo.family/tokens/base/0x123",
        "image_url": "https://images.example.com/example.png",
        "ai_discovery_since": "2026-11-05T00:00:00Z",
        "is_favorite": False,
        "score": 75,
        "risk_score": 20,
        "data_completeness_percent": 80,
        "market_cap": 50_000,
        "ai_rmc": 25_000,
        "ai_rh": 60_000,
        "liquidity": 10_000,
        "buyer_count": 7,
        "fomo_holder_count": 12,
    }
    assert "evaluation_report" not in payload
    assert "model" not in payload
    assert "job_id" not in payload
    assert requests == [("GET", public_ai_discovery.TRACKER_API_URL)]


def test_public_app_exposes_no_tracker_management_or_mutation_routes() -> None:
    client = TestClient(public_ai_discovery.app)

    assert client.get("/api/v1/tokens/").status_code == 404
    assert client.get("/api/ai-discovery/run").status_code == 404
    assert client.post("/api/ai-discovery/run").status_code == 404
    assert client.post("/api/ai-discovery/candidates/1").status_code == 404
    assert client.post("/api/ai-discovery").status_code == 405
    assert client.delete("/api/ai-discovery/candidates/1").status_code == 404
    assert client.delete("/api/ai-discovery/top-picks/1").status_code == 404
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_refresh_reads_status_endpoint_only_and_never_queues_discovery(monkeypatch) -> None:
    requests: list[tuple[str, str]] = []
    install_tracker_response(monkeypatch, SAMPLE_STATUS, requests)
    response = TestClient(public_ai_discovery.app).get("/api/ai-discovery")

    assert response.status_code == 200
    assert requests == [("GET", public_ai_discovery.TRACKER_API_URL)]
    assert all("/run" not in url for _, url in requests)
    script = Path(public_ai_discovery.ASSETS_DIR / "app.js").read_text()
    assert "window.setInterval(loadCandidates, 60_000)" in script
    assert "navigator.clipboard.writeText(button.dataset.copyAddress)" in script
    assert "copyButton.dataset.copyAddress = token.contract_address" in script
    assert "copy-address" in script
    assert "token.fomo_url" in script
    assert "https://fomo.family/tokens/" in script
    assert "nameLink.href = tokenUrl" in script


def test_empty_candidates_are_returned_as_an_empty_list(monkeypatch) -> None:
    requests: list[tuple[str, str]] = []
    empty_status = {**SAMPLE_STATUS, "candidate_count": 0, "candidates": []}
    install_tracker_response(monkeypatch, empty_status, requests)

    response = TestClient(public_ai_discovery.app).get("/api/ai-discovery")

    assert response.status_code == 200
    assert response.json()["candidate_count"] == 0
    assert response.json()["candidates"] == []


def test_unavailable_tracker_is_reported_without_internal_details(monkeypatch) -> None:
    class OfflineAsyncClient:
        def __init__(self, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            pass

        async def get(self, *_args, **_kwargs):
            raise httpx.ConnectError("private host and credentials")

    monkeypatch.setattr(public_ai_discovery.httpx, "AsyncClient", OfflineAsyncClient)
    response = TestClient(public_ai_discovery.app).get("/api/ai-discovery")

    assert response.status_code == 503
    assert response.json()["detail"] == "The local tracker API is unavailable."
    assert "credentials" not in response.text


def test_malformed_tracker_rows_are_rejected(monkeypatch) -> None:
    requests: list[tuple[str, str]] = []
    invalid_status = {**SAMPLE_STATUS, "candidates": [{"name": "Missing rank"}]}
    install_tracker_response(monkeypatch, invalid_status, requests)
    response = TestClient(public_ai_discovery.app).get("/api/ai-discovery")

    assert response.status_code == 502
    assert response.json()["detail"] == "The local tracker returned invalid AI Discovery data."
