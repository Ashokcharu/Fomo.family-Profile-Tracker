# Fomo Family Profile Tracker

Fomo Family Profile Tracker is a local on-chain research dashboard. It tracks tokens and Fomo Family trader activity, records market history, compares trader picks, and ranks active tracked tokens for review.

The web dashboard and FastAPI service run together at `http://localhost:8000`. The default database is a local SQLite file; PostgreSQL and Redis are optional for local development.

## What You Can Do

- **The Tokens:** Add contracts and review market cap, liquidity, holders, followed-trader buyers, and growth.
- **Traders:** Follow Fomo Family profiles and review their qualifying picks, historical growth, success rates, and risk estimates.
- **AI Discovery:** Analyze only active tokens from The Tokens, then review ranked candidates, evidence, risks, and local-model assessment.
- **Favorites and research views:** Save tokens for later and review graduations, risk findings, or dismissed tokens separately.

## Setup

### Prerequisites

- Python 3.12 or newer
- Git
- Ollama with `qwen3:8b` for local-model assessments (optional; signal-based ranking still works without it)
- Chrome and a Fomo Family sign-in for automatic trader-activity monitoring (optional)

### Install and run

```bash
git clone <your-repository-url>
cd FomoProfleTracker
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[scraper]"
cp .env.example .env
ollama pull qwen3:8b
python main.py
```

Open `http://localhost:8000`. Interactive API documentation is available at `http://localhost:8000/docs`.

On startup, the app creates missing database tables. With the example settings, it stores data in `app.db` and starts the Fomo Family browser monitor. Sign in to Fomo Family in the opened browser when prompted. Set `FOMO_ACTIVITY_MONITOR_ENABLED=false` in `.env` to run without browser monitoring.

## How the App Works

1. Add contract addresses in **The Tokens** and usernames in **Traders**.
2. The browser monitor checks followed Fomo Family profiles and records new buy events. Duplicate swaps are ignored.
3. Every `TOKEN_REFRESH_INTERVAL_SECONDS` (300 seconds by default), the market worker fetches token metrics from DexScreener and stores snapshots. These snapshots support growth, graduation, and risk calculations. Fomo holder data is included when the authenticated browser monitor can provide it.
4. Trader performance uses each trader-token buy relationship separately. A qualifying pick starts with the first detected buy below $50,000 market cap; its original reached market cap (RMC) is preserved while the highest later observation (RH) is tracked. The score estimates token-picking performance, not realized trading profit.
5. Select **Analyze tracked tokens** in **AI Discovery** to queue a background scan. It considers active tokens from The Tokens only; favorited, dismissed, graduated, or risk-flagged tokens are excluded. It ranks up to 30 candidates from market-cap fit, liquidity, followed-trader buyers, observed growth, and holder count.
6. Ollama (`qwen3:8b` by default) reviews the top signal candidate and its score is blended into the ranking. Other candidates retain their transparent signal scores. The tab updates while the scan runs. If Ollama is unavailable or returns an invalid assessment, signal-ranked results are still available.

AI output is a research aid, not financial advice, a price prediction, or a statement of realized returns. The local 8B model may take several minutes depending on hardware and tracked-token volume.

## Configuration

Copy `.env.example` to `.env` and edit local settings there. Keep real credentials and machine-specific values out of Git.

- `DATABASE_URL` defaults to SQLite at `sqlite:///./app.db`.
- `FOMO_ACTIVITY_MONITOR_ENABLED` controls browser-based Fomo Family monitoring.
- `FOMO_ACTIVITY_BROWSER_PROFILE_DIR` selects the local browser profile directory.
- `TOKEN_REFRESH_INTERVAL_SECONDS` controls market snapshot frequency.
- `AI_DISCOVERY_ENABLED`, `OLLAMA_BASE_URL`, and `OLLAMA_MODEL` control AI Discovery.
- `HELIUS_API_KEYS` is optional; Helius does not provide this app's market cap or liquidity data.

For optional PostgreSQL and Redis containers, run `docker compose up -d postgres redis`. To use the Compose PostgreSQL service, set `DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/meme_intelligence` in `.env`. SQLite remains the simplest local setup.

Set `PORT` to change the web port, for example `PORT=8001 python main.py`.

## Tests

```bash
python -m pytest -q
```

