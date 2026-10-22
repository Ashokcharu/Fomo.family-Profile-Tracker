# Fomo Profile Tracker

Fomo Profile Tracker is a local on-chain research dashboard for following Fomo Family traders, tracking token market data, comparing trader picks, and ranking tracked tokens with AI-assisted analysis.

The FastAPI backend and browser dashboard run together at `http://localhost:8000`. SQLite is the default database, so PostgreSQL and Redis are not required for a local setup.

## Features

- **The Tokens:** Track contracts across supported chains and review current market cap, liquidity, holders, followed-trader buyers, and growth.
- **Traders:** Follow Fomo Family profiles, record their buys, and compare historical picks using success rates, growth, and failure estimates.
- **AI Discovery:** Rank active tokens from The Tokens using tracked market signals. Ollama (`qwen3:8b` by default) assesses the top signal candidate; other candidates retain transparent signal scores. Results are saved for the AI Discovery tab.
- **Research views:** Keep favorites, graduated tokens, risk findings, and dismissed tokens separate from active tracking.

## How It Works

1. Add token contracts in **The Tokens**, and add Fomo Family usernames in **Traders**.
2. When the browser monitor is enabled, it opens or attaches to a local Chrome profile, reads followed profiles, and records new buy activity. Sign in to Fomo Family in that browser when prompted. Duplicate swap events are ignored.
3. The market refresh worker requests token metrics from DexScreener every `TOKEN_REFRESH_INTERVAL_SECONDS` (300 seconds by default). It stores snapshots and updates token growth and graduation/risk tracking. Fomo holder data comes from the authenticated browser monitor when available.
4. **AI Discovery** analyzes only active tokens in The Tokens. It excludes tokens that are favorited, dismissed, graduated, or currently flagged for risk, then ranks up to 30 using market-cap fit, liquidity, followed-trader buyers, observed growth, and holder count.
5. The scan runs as a background job. The signal-ranked candidates appear while the local model assesses the top candidate. Its score is blended with the tracked-signal score, then the list is reranked. The tab checks for completion automatically. If Ollama is unavailable or returns an invalid assessment, the signal score remains available.

AI results are research signals, not price predictions or realized trading returns. A local 8B model can take several minutes, depending on hardware and the size of the tracked-token list.

## Quick Start

Requirements: Python 3.12 or newer, Git, and (for model-assisted discovery) Ollama with `qwen3:8b` installed.

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

Open `http://localhost:8000`. The API documentation is at `http://localhost:8000/docs`.

The `.env.example` defaults to SQLite at `./app.db`, enables AI Discovery, and enables the Fomo browser monitor. To run without the monitor, override it in the shell:

```bash
FOMO_ACTIVITY_MONITOR_ENABLED=false python main.py
```

To use a different port, set `PORT`, for example `PORT=8001 python main.py`.

## Configuration

- `DATABASE_URL`: SQLite by default. The Compose file also provides optional PostgreSQL and Redis services; PostgreSQL can be selected with `postgresql+psycopg://postgres:postgres@localhost:5432/meme_intelligence` after starting the `postgres` service.
- `FOMO_ACTIVITY_MONITOR_ENABLED`: Enables browser-based Fomo Family following and buy monitoring. The monitor uses the local browser profile configured by `FOMO_ACTIVITY_BROWSER_PROFILE_DIR`.
- `TOKEN_REFRESH_INTERVAL_SECONDS`: Market snapshot interval; default is 300 seconds.
- `AI_DISCOVERY_ENABLED`: Enables AI Discovery.
- `OLLAMA_BASE_URL` and `OLLAMA_MODEL`: Local Ollama URL and model. The example uses `http://localhost:11434` and `qwen3:8b`.
- `HELIUS_API_KEYS`: Optional provider keys. Helius is not the source of market cap or liquidity in this app.

Keep actual credentials and machine-specific overrides in `.env`; do not commit them.

## Tests

Activate the virtual environment, then run:

```bash
python -m pytest -q
```

## GitHub Hygiene

`.gitignore` excludes `.env` files, SQLite/database files, virtual environments, Python caches, and the entire local `data/` directory (including browser profiles and follower state). `.env.example` is the safe configuration template to commit. If local data or credentials were committed previously, `.gitignore` alone will not remove them from Git history; untrack them and rotate any exposed credentials before pushing.