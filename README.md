# Fomo Family Profile Tracker

Fomo Family Profile Tracker is a local research dashboard for tracking token contracts and public Fomo Family trader activity. It stores observations in a database, calculates historical growth measures, and separates the results into focused review tabs.

This is a tracker, not a full-chain token crawler or a trading system. It does not discover every token created on a chain, execute trades, or calculate realized profit. A token enters tracking when you add its contract or when an observed qualifying buy from a followed trader adds it.

## Contents

- [What the application does](#what-the-application-does)
- [Quick start](#quick-start)
- [How data enters the application](#how-data-enters-the-application)
- [How tokens are analyzed](#how-tokens-are-analyzed)
- [Dashboard tabs](#dashboard-tabs)
- [Metrics and terminology](#metrics-and-terminology)
- [Architecture and file guide](#architecture-and-file-guide)
- [Python libraries](#python-libraries)
- [Configuration](#configuration)
- [Database and background work](#database-and-background-work)
- [API and tests](#api-and-tests)
- [Limitations and safety notes](#limitations-and-safety-notes)

## What the Application Does

The application has three cooperating parts:

1. **Collection:** you can enter token contracts and Fomo Family usernames. When enabled, a Playwright-controlled Chrome session reads profile activity and Fomo Family token metadata. DexScreener supplies market-pair statistics for tracked tokens.
2. **Storage and calculations:** SQLAlchemy stores the records in SQLite or PostgreSQL. Background loops save market snapshots and update graduation, risk, and followed-trader performance data.
3. **Review:** a small plain HTML/CSS/JavaScript interface calls the FastAPI endpoints and displays the stored records in separate tabs. AI Discovery ranks the active tracked-token set using signals; an optional local Ollama model can assess one leading candidate during a manual run.

The browser UI is served at `http://localhost:8000`. FastAPI's interactive API reference is at `http://localhost:8000/docs`.

## Quick Start

### Requirements

- Python 3.12 or newer.
- Git.
- For Fomo Family activity collection: Google Chrome or Chromium, a Fomo Family account signed in to the monitor's browser profile, and the optional Playwright package.
- For model-assisted AI Discovery: Ollama installed and running, plus the configured model (default `qwen3:8b`). This is optional; signal-based ranking does not require Ollama.

### Install

```bash
git clone <your-repository-url>
cd FomoProfleTracker
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[scraper]"
python -m playwright install chromium
cp .env.example .env
```
Edit `.env` for your machine. The example enables browser monitoring and includes placeholder Helius keys; set `FOMO_ACTIVITY_MONITOR_ENABLED=false` if you do not want browser monitoring, and remove or replace placeholder credentials. The default database is a local SQLite file at `./app.db`. The `python main.py` launcher enables the Fomo Family monitor unless an environment setting overrides it. To run without browser monitoring:

```bash
FOMO_ACTIVITY_MONITOR_ENABLED=false python main.py
```

To use the optional model-assisted path:

```bash
ollama pull qwen3:8b
ollama serve
```

In another terminal, start the application:

```bash
python main.py
```

Open `http://localhost:8000`. If the Fomo Family monitor opens a browser window, sign in there; the monitor stores its browser profile under `~/.cache/fomo-following` by default so the session can persist. The app creates missing database tables on startup.

To use PostgreSQL instead of SQLite, start the database service and set a connection string before starting the app:

```bash
docker compose up -d postgres
DATABASE_URL='postgresql+psycopg://postgres:postgres@localhost:5432/meme_intelligence' python main.py
```

Redis can also be started with `docker compose up -d redis`, but the current application background loops do not use Redis as their queue. See [Database and background work](#database-and-background-work).

## How Data Enters the Application

There are two main collection paths. Neither one scans every chain for new contracts.

```mermaid
flowchart TD
    A[User adds chain and contract] --> C[Token record in database]
    B[User follows a Fomo Family username] --> D{Monitor enabled?}
    D -- No --> E[Username remains in watchlist]
    D -- Yes --> F[Playwright connects to signed-in Chrome]
    F --> G[Read profile and swap API responses]
    G --> H[Validate profile identity and buy swaps]
    H --> I[Resolve market cap from swap, Fomo metadata, or DexScreener]
    I --> J{Buy is under discovery threshold?}
    J -- Yes --> K[Create or match token and save buy event]
    J -- No or data missing --> L[Do not add as a qualifying discovered buy]
    K --> C
    C --> M[Scheduled market refresh]
    M --> N[DexScreener batch metrics, with Fomo metadata fallback]
    N --> O[Append market observation]
    O --> P[Update trader picks, graduation and risk]
```

### 1. Manual contract tracking

In **The Tokens**, select **Add token**, choose a chain name, and enter the contract address. The browser sends a request to `POST /api/v1/tokens/`. The token service normalizes its identity using the chain adapter and creates the database record if it does not already exist. Market metrics are filled in by later refreshes; adding a contract does not itself prove that the contract is valid or safe.

### 2. Fomo Family profile activity

Add a username in **Traders**. If monitoring is enabled, the monitor uses Playwright to connect to a local Chrome/Chromium browser through Chrome DevTools Protocol (CDP). It opens Fomo Family pages and observes responses from the Fomo Family web API while that page loads. The code checks that the profile response matches the requested username, then extracts swap data associated with that profile.

The swap parser accepts buy events with an ID, token address, network ID, and parseable timestamp. It rejects other sides and incomplete events. Each accepted swap is assigned a deduplication key based on its provider swap ID, so polling the same activity again does not insert it twice. Qualifying buys create or match a token, a trader transaction event, and a trader-token pick.

The monitor can also request Fomo Family token metadata in batches. It stores available name, symbol, image, holder count, supply, price, market cap, liquidity, and volume values. This route is dependent on the signed-in browser session and the Fomo Family website/API response shape. When an automatic following-list sync URL is configured, it can read the visible following list and reconcile the usernames into the watchlist.

### 3. Market snapshots

The market refresh worker selects tokens already in the database, groups them by provider chain, and requests DexScreener metrics in batches of up to 30 addresses. If batch results omit a token, the worker makes a limited rotating set of individual fallback requests. Requests are run concurrently; results are matched to the requested chain and address, and the pair with the greatest USD liquidity is selected where more than one pair matches.

For each token with usable market data, the worker appends a `MarketObservation`. It carries market cap (or FDV fallback), USD price, USD liquidity, 24-hour volume, provider, social data, holder values, and raw pair data when available. Fresh Fomo Family metadata can fill fields that DexScreener did not provide. A later observation never overwrites the earlier snapshot, which is what allows the app to calculate historical growth.

## How Tokens Are Analyzed

### Market and event calculations

The market refresh uses the first valid market-cap observation at or after tracking began as the tracked baseline. If no such observation exists, it can use the first eligible discovery event. It then updates:

- Current and highest observed market cap since tracking.
- Growth from the tracked baseline.
- Followed-trader pick records for traders whose first detected buy was below `$50,000` market cap.
- Graduation events when market cap reaches the configured graduation threshold (default `$50,000`).
- A risk finding when current market cap is at least 90% below the tracked baseline.

The market worker excludes actively dismissed tokens from normal refresh. Tokens already at or above the graduation threshold at the time tracking begins are not treated as successful graduations; they are dismissed from active review. Details can be inspected in the corresponding service modules in `app/features/`.

### Followed-trader performance

One `TraderTokenPick` represents one trader-token pair, not a completed trade. The first qualifying buy market cap becomes that pick's baseline. Later scheduled observations update its current value and its highest observed value. The trader table summarizes the maximum observed growth multiple for each pick and derives 2x/5x/10x rates, average and median growth, and a confidence-adjusted score.

Failure rate is deliberately unavailable for immature picks. A pick must have at least three observations spanning at least seven days. A mature pick counts as a failure if current market cap is at or below 10% of its baseline, or if its observed high never reached 1.2x. This is a historical signal measure, not wallet P&L or proof that the trader sold at the high.

### AI Discovery

```mermaid
flowchart TD
    A[Automatic timer or Analyze tracked tokens button] --> B[Load active tokens from The Tokens]
    B --> C[Build features from latest market and trader data]
    C --> D[Apply heuristic score, prior feedback and continuation adjustment]
    D --> E[Keep up to 30 ranked candidates]
    E --> F{Manual run and Ollama available?}
    F -- No --> G[Return signal-ranked candidates]
    F -- Yes --> H[Send leading candidate to local Ollama]
    H --> I[Validate model assessment and combine scores]
    I --> J[Save run, baseline, candidate details and job status]
    G --> J
    J --> K[AI Discovery tab]
    J --> L[Update eligible Top 3 picks]
```

AI Discovery only analyzes the active tokens in **The Tokens**. It does not search the entire blockchain. For each tracked token, its explainable signal score considers:

- Fit to the configured market-cap range and discovery ceiling.
- Liquidity relative to the configured minimum.
- Number of followed traders with recorded buys.
- Growth from the tracked baseline and early positive momentum.
- Fomo Family holder count, when available.
- Evidence strings for strengths, missing data, and risks.

The automatic scan is signal-only and does not call Ollama. It runs on its own timer, separate from market refresh. Clicking **Analyze tracked tokens** creates a database background-job record and runs a manual scan. When the model is available, the app asks it to assess the leading signal candidate and blends the model score with the heuristic score; when unavailable or invalid, signal-ranked results remain usable. The LLM is not trained on these results. Candidate feedback and observed outcomes modify ranking adjustments; they do not retrain the model.

The app retains up to 30 candidates for review. Top 3 picks are a stricter subset, with no more than three active entries. A pick must move positively above both the AI baseline and the original tracked baseline, remain within the configured early-growth window (up to 2x from each baseline), and meet the confidence threshold. Picks remain pinned until their observed high reaches 10x their AI baseline or they are manually removed. AI RMC (AIRMC) is the saved market-cap reference for the candidate's first AI appearance; AI RH (AIRH) is the highest later observation used by the discovery view.

## Dashboard Tabs

Tabs are frontend views over API responses; most tabs do not scrape data when opened. Collection happens in background workers or when a user submits a form. The UI code is in `frontend/index.html`, `frontend/app.js`, and `frontend/styles.css`.

| Tab | API used | What it shows / where the data comes from |
| --- | --- | --- |
| **Overview** | `GET /api/v1/tokens/recent-bought` | Tokens with recent recorded Fomo Family buys, ordered by latest buy. It is a short activity list, not a token discovery crawler. |
| **The Tokens** | `GET /api/v1/tokens/`, `GET /api/v1/tokens/count` | Active tracked contracts with latest market values, holders when available, baseline/high growth, buyer counts, links, and favorite state. Add and delete actions modify tracking records. |
| **Traders** | `GET /api/v1/followed-traders/`, `GET /api/v1/health` | Followed usernames, monitor status, qualifying pick counts, growth rates, and score. The user can add or remove profiles and choose a performance sort. |
| **Favorites** | `GET /api/v1/favorites/` | Tokens explicitly bookmarked for research. The record keeps market values from the time it was favorited as well as current display data. |
| **Graduated** | `GET /api/v1/graduated-tokens/` | Tracked tokens whose observed market cap crossed the configured threshold, with milestone and high values. |
| **Risk signals** | `GET /api/v1/risk-analysis/` | Persisted findings such as a severe decline from the tracked market-cap baseline. This is rule-based evidence, not a full contract audit. |
| **AI Discovery** | `GET /api/v1/ai-discovery/`; run with `POST /api/v1/ai-discovery/run` | Ranked active tracked tokens, score breakdown, feedback, model status, strengths, and risks. Opening the tab reads current results; the button queues a scan. |
| **Top 3 picks** | `GET /api/v1/ai-discovery/` | The active, eligible subset of AI candidates. Removing a pick does not delete its token. |
| **Dismissed** | `GET /api/v1/dismissed-tokens/` | Historical dismissal records and reasons. Active dismissals are excluded from active review/refresh until restored through the relevant action. |

Other API groups include `/api/v1/dashboard/overview`, `/api/v1/discoveries/`, and `/api/v1/market-data/history`. FastAPI lists the complete route schemas at `/docs` while the server is running.

## Metrics and Terminology

- **MC:** latest observed market cap. The provider's fully diluted valuation (FDV) may be used when market cap is absent.
- **RMC:** reached market cap, the first eligible market-cap value observed after tracking began. It is the tracking baseline, not necessarily the token's true launch value or a trader's exact entry valuation.
- **RH:** highest market cap observed since tracking began.
- **RMC to MC:** current market cap divided by RMC. For example, `2.0x` means the current observed market cap is twice the stored baseline.
- **Fomo holders (FH):** holder count obtained from Fomo Family metadata when that data is available. It is not guaranteed to be present or current for every token.
- **Buyer count:** distinct followed traders with a recorded buy for that token, not the number of all token holders or all on-chain wallets.
- **Graduated:** crossed the configured market-cap threshold after an eligible baseline was established. Default threshold: `$50,000`.
- **AI RMC / AIRMC:** market-cap reference stored when the token first appears in AI candidate tracking. AI growth is measured from this reference.
- **AI RH / AIRH:** highest market-cap observation since AI candidate tracking began.
- **Trader success rate:** fraction of qualifying trader-token picks whose observed high reached the stated multiple. It is not a realized return statistic.

## Architecture and File Guide

### Application entry points and shared infrastructure

| File | Responsibility |
| --- | --- |
| `main.py` | Repository launcher. Reads `PORT` and `HOST`, enables the Fomo monitor by default unless overridden, and calls the server runner. |
| `scripts/server.py` | Starts the ASGI server with Uvicorn and the application object. |
| `scripts/start.sh` | Simple shell entry point that runs `python main.py`. |
| `app/main.py` | Creates the FastAPI app, registers routers, serves the frontend, creates database tables on startup, performs startup reconciliation, and manages background task lifetimes. |
| `app/config.py` | Defines settings, defaults, `.env` loading, validation, and cached access through `get_settings()`. |
| `app/database.py` | Creates the SQLAlchemy engine and session factory, configures SQLite behavior, and provides the request-scoped database dependency. |
| `app/__init__.py` | Marks `app` as a Python package. |
| `pyproject.toml` | Python version, direct dependencies, optional dependency groups, build metadata, and pytest configuration. |
| `alembic.ini` | Alembic migration tool configuration. |
| `migrations/env.py` | Alembic environment setup. The current startup path creates tables with SQLAlchemy; migration revisions are not included in this checkout. |
| `docker-compose.yml` | Local PostgreSQL, Redis, and app service definitions. The README's local PostgreSQL instructions start only the database service. |

### Chain adapters

The chain adapters provide chain-specific address normalization and transaction parsing behind a shared interface. The currently registered adapters are Solana, Robinhood Chain, Base, BNB Chain, Ethereum, and Monad. Provider coverage is not identical to adapter registration: market data still depends on the external provider recognizing a chain/address.

| File | Responsibility |
| --- | --- |
| `app/chains/base.py` | Abstract `ChainAdapter` contract: address normalization and transaction-event parsing. |
| `app/chains/registry.py` | Stores adapters by chain name and provides lookup. |
| `app/chains/__init__.py` | Instantiates and registers the supported adapters. |
| `app/chains/solana/adapter.py` | Solana address normalization and transaction payload mapping. |
| `app/chains/solana/__init__.py` | Exposes the Solana adapter. |
| `app/chains/robinhood_chain/adapter.py` | Robinhood Chain adapter implementation. |
| `app/chains/robinhood_chain/__init__.py` | Exposes the Robinhood Chain adapter. |
| `app/chains/base_chain/adapter.py` | Base network adapter implementation. |
| `app/chains/base_chain/__init__.py` | Exposes the Base adapter. |
| `app/chains/bnb_chain/adapter.py` | BNB Chain adapter implementation. |
| `app/chains/bnb_chain/__init__.py` | Exposes the BNB adapter. |
| `app/chains/ethereum/adapter.py` | Ethereum adapter implementation. |
| `app/chains/ethereum/__init__.py` | Exposes the Ethereum adapter. |
| `app/chains/monad/adapter.py` | Monad adapter implementation. |
| `app/chains/monad/__init__.py` | Exposes the Monad adapter. |

### Feature modules

Each feature normally has a `routes.py` file for HTTP endpoints and a `service.py` file for database queries and business rules. The routes validate/request data and call the service; the services keep the behavior out of the frontend.

| File | Responsibility |
| --- | --- |
| `app/features/dashboard/routes.py` | Exposes dashboard summary endpoints. |
| `app/features/dashboard/service.py` | Counts tokens, traders, buys, favorites, graduations, risks, and dismissals for the overview summary. |
| `app/features/tokens/routes.py` | Token list/count, recent buys, buyer detail, add, and delete endpoints. |
| `app/features/tokens/service.py` | Token identity, active-token filtering, token display values, market history-derived fields, and token deletion. |
| `app/features/tokens/__init__.py` | Re-exports shared token creation and identity helpers. |
| `app/features/discovery/routes.py` | Exposes discovery-event listing. |
| `app/features/discovery/service.py` | Stores and lists discovery events and includes the explainable signal-score helper. |
| `app/features/followed_users/routes.py` | Followed-trader, activity, health-linked monitor, and user-management endpoints. |
| `app/features/followed_users/service.py` | Normalizes usernames, creates and reconciles profiles, lists/sorts users, and deletes associated records. |
| `app/features/followed_users/performance.py` | Creates and updates trader-token picks, backfills picks, and calculates growth, success, failure, and confidence-adjusted statistics. |
| `app/features/market_data/routes.py` | Exposes market-observation history. |
| `app/features/market_data/service.py` | Lists and creates market observations and provides provider-priority helpers. |
| `app/features/favorites/routes.py` | Favorite listing, count, create, and delete endpoints. |
| `app/features/favorites/service.py` | Stores a favorite with values captured at save time and lists/deletes favorites. |
| `app/features/graduated_tokens/routes.py` | Graduated-token listing and count endpoints. |
| `app/features/graduated_tokens/service.py` | Checks thresholds, reconciles old events, tracks graduation progress, and lists milestone records. |
| `app/features/risk_analysis/routes.py` | Risk-finding listing, count, and deletion endpoints. |
| `app/features/risk_analysis/service.py` | Lists findings and records the market-cap drawdown rule. |
| `app/features/dismissed_tokens/routes.py` | Dismissed-token list, count, bulk dismiss, restore, and delete endpoints. |
| `app/features/dismissed_tokens/service.py` | Determines active dismissals and changes dismissal state. |
| `app/features/ai_discovery/routes.py` | AI status, manual run, feedback, and Top 3 removal endpoints. |
| `app/features/ai_discovery/service.py` | Builds and ranks discovery runs, manages job state and baselines, combines optional model results, feedback, and top picks. |
| `app/features/ai_discovery/candidate_features.py` | Converts a token's market, buyer, holder, and growth data into scores and human-readable strengths/risks. |
| `app/features/ai_discovery/historical_analysis.py` | Historical AI-discovery analysis helpers. |
| `app/features/ai_discovery/manual_data.py` | Helpers for manually supplied AI-discovery data. |
| `app/features/ai_discovery/model_interface.py` | Placeholder interface for future model implementations; current inference calls the Ollama client directly. |
| `app/features/ai_discovery/schemas.py` | Pydantic request schemas for AI Discovery feedback and related values. |

### External integrations and workers

| File | Responsibility |
| --- | --- |
| `app/integrations/dexscreener/client.py` | HTTP client for single-token and batched DexScreener lookups; chooses the matching chain/address pair and extracts market/social metrics. |
| `app/integrations/fomo_family/monitor.py` | Playwright/CDP browser lifecycle, scheduled trader polling, Fomo profile/following response capture, token metadata collection, and persistence coordination. |
| `app/integrations/fomo_family/activity.py` | Parses Fomo profile and buy-swap data, resolves buy market caps, deduplicates and stores activity, and stores token metadata observations. |
| `app/integrations/fomo_family/client.py` | Minimal API-client placeholder. Its current sync methods report that manual/browser sync is required; it is not the working browser scraper. |
| `app/integrations/helius/client.py` | Helius API-key rotation scaffold. The current wallet-activity method returns an empty transaction list; Helius is not the source of market cap or liquidity here. |
| `app/integrations/ollama/client.py` | Checks a local Ollama server and submits JSON-format generation requests with configured model/runtime limits. |
| `app/integrations/ollama/prompts.py` | Builds the prompt sent to Ollama for a discovery assessment. |
| `app/integrations/other_providers/__init__.py` | Package placeholder for additional provider integrations. |
| `app/workers/market_refresh.py` | Refreshes active-token market metrics, appends observations, updates picks, graduations, and risk findings. |
| `app/workers/ai_discovery.py` | Runs the periodic signal-only AI Discovery scan and skips it while a manual scan is active. |
| `app/workers/scheduler.py` | Scheduling support for recurring jobs. The main application currently starts its market and AI loops directly from `app/main.py`. |

### Data models and schemas

| File | Responsibility |
| --- | --- |
| `app/models/records.py` | SQLAlchemy table definitions for chains, tokens, observations, traders, buys, picks, favorites, graduations, risks, AI records, jobs, and audit/checkpoint records. |
| `app/models/__init__.py` | Re-exports model classes for convenient imports and metadata registration. |
| `app/schemas/common.py` | Shared API schemas for health and pagination values. |
| `app/schemas/system.py` | System/configuration response schemas. |
| `app/schemas/token.py` | Token create/read and discovery result schemas. |
| `app/schemas/trader.py` | Followed-trader create/read schemas. |

### Frontend and tests

| File | Responsibility |
| --- | --- |
| `frontend/index.html` | The served page structure, navigation tabs, forms, tables, and dialogs. |
| `frontend/app.js` | Tab definitions, API requests, rendering, search/pagination, and user actions. It uses browser-native JavaScript and `fetch`, not a frontend framework. |
| `frontend/styles.css` | Layout, colors, responsive behavior, and table/dialog styling. |
| `frontend/src/api/client.ts` | A small TypeScript API-client file; it is not loaded by the served page, which uses `frontend/app.js`. |
| `tests/test_app_core.py` | Core application and API behavior tests. |
| `tests/test_ai_discovery.py` | AI candidate scoring, scan, feedback, and top-pick behavior tests. |
| `tests/test_fomo_profile_scraper.py` | Fomo profile, swap parsing, metadata, and scraper behavior tests. |
| `tests/test_provider_and_graduation.py` | Provider data and graduation calculations/tests. |
| `tests/test_provider_rotation_and_followed_users.py` | Provider key rotation and followed-user/performance behavior tests. |
| `tests/test_token_service.py` | Token identity, listing, display, and service behavior tests. |
| `tests/test_root_ui.py` | Root page, static frontend, health endpoint, and legacy page checks. |
| `test.py` | Standalone test/helper script at repository root. |

## Python Libraries

These are the direct packages declared in `pyproject.toml`. Their transitive dependencies are installed by pip as needed.

| Library | Required? | What it does here |
| --- | --- | --- |
| **FastAPI** (`fastapi`) | Yes | Defines HTTP routes, parses request data, injects database dependencies, validates response/request schemas, serves OpenAPI docs, and runs startup/shutdown lifecycle hooks. |
| **Uvicorn** (`uvicorn[standard]`) | Yes | ASGI web server that hosts FastAPI and serves the UI/API over HTTP. |
| **SQLAlchemy** (`sqlalchemy`) | Yes | ORM and SQL toolkit: maps Python model classes to database tables, builds queries, and manages database sessions and transactions. |
| **Alembic** (`alembic`) | Yes | Database migration tool. Configuration/environment files exist; this checkout currently creates tables at startup and has no migration revision files. |
| **Psycopg** (`psycopg[binary]`) | For PostgreSQL | PostgreSQL database driver used by SQLAlchemy when `DATABASE_URL` points to PostgreSQL. SQLite is the default and does not need this driver. |
| **Pydantic** (`pydantic`) | Yes | Validates and serializes typed request, response, and configuration data. |
| **Pydantic Settings** (`pydantic-settings`) | Yes | Builds the settings object from defaults, environment variables, and the optional `.env` file. |
| **python-dotenv** (`python-dotenv`) | Yes | Enables reading `.env` configuration files through the settings stack. |
| **HTTPX** (`httpx`) | Yes | Makes HTTP requests to DexScreener and the local Ollama API, with response and timeout handling. |
| **Celery** (`celery`) | Declared, not used by current loops | Distributed task-queue library. The running app currently launches its recurring tasks with `asyncio` and FastAPI background tasks instead of a Celery worker. |
| **Redis** (`redis`) | Declared, optional service | Redis client package and optional Compose service. The current market/AI loops do not use Redis as their broker or result store. |
| **python-multipart** (`python-multipart`) | Yes in declared dependencies | Supports multipart form parsing in FastAPI. The primary dashboard requests use JSON/query parameters. |
| **Playwright** (`playwright`) | Optional `scraper` extra | Automates and controls Chromium through CDP for the signed-in Fomo Family browser collection path. Install the browser binary with `python -m playwright install chromium`. |
| **pytest** (`pytest`) | Test dependency | Runs the repository's automated tests. |
| **pytest-asyncio** (`pytest-asyncio`) | Test dependency | Provides pytest support for asynchronous tests. |
| **Ruff** (`ruff`) | Optional `dev` extra | Python linter and formatter for development checks. |
| **mypy** (`mypy`) | Optional `dev` extra | Static type checker for development checks. |

The backend also uses Python standard-library modules: `asyncio` for task loops, `concurrent.futures` for parallel provider requests, `datetime` for timestamps, `logging` for diagnostics, `math` and `statistics` for score/performance calculations, `pathlib` for paths, `urllib.parse` for URL handling, and modules such as `json`, `typing`, `functools`, `contextlib`, `abc`, `subprocess`, `shutil`, `re`, and `uuid` for their standard utility roles. These are included with Python and are not installed with pip.

## Configuration

Settings are defined in `app/config.py`. Values can be supplied as environment variables or in a local `.env` file. Variable names are case-insensitive; unknown settings are ignored.

| Setting | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite:///./app.db` | SQLAlchemy database connection. |
| `REDIS_URL` | `redis://localhost:6379/0` | Reserved/configured Redis URL; current background loops do not use it. |
| `PORT` | `8000` | HTTP port used by `python main.py`. |
| `HOST` | `0.0.0.0` | Bind address used by `python main.py`. |
| `FOMO_ACTIVITY_MONITOR_ENABLED` | Settings default `false`; launcher sets `true` unless already set | Enables browser-based profile and metadata monitoring. Set to `false` to avoid launching/connecting to Chrome. |
| `FOMO_ACTIVITY_BROWSER_PROFILE_DIR` | `~/.cache/fomo-following` | Browser profile directory for the monitor's sign-in session. |
| `FOMO_ACTIVITY_CDP_URL` | `http://127.0.0.1:9222` | Chrome DevTools Protocol endpoint. |
| `FOMO_ACTIVITY_POLL_INTERVAL_SECONDS` | `300` | Delay between activity polling cycles. |
| `FOMO_FAMILY_PROFILE_URL` | Empty | Optional HTTPS profile URL whose following list can be synchronized. |
| `FOMO_FAMILY_PROFILE_HANDLE` | Empty | Optional profile/referral handle used by the monitor. |
| `FOMO_FOLLOWING_SYNC_INTERVAL_SECONDS` | `43200` | Following-list synchronization interval. |
| `TOKEN_REFRESH_INTERVAL_SECONDS` | `300` | Market-observation refresh interval. |
| `DISCOVERY_THRESHOLD_USD` | `50000` | Maximum market cap for qualifying tracked discovery buys. |
| `DISCOVERY_PREFERRED_MIN_USD` | `10000` | Lower edge of the AI preferred market-cap band. |
| `DISCOVERY_PREFERRED_MAX_USD` | `30000` | Upper edge of the AI preferred market-cap band. |
| `GRADUATION_THRESHOLD_USD` | `50000` | Market cap required to record graduation. |
| `MINIMUM_LIQUIDITY_USD` | `5000` | Liquidity reference used by discovery scoring. |
| `AI_DISCOVERY_ENABLED` | `true` | Enables automatic and manual AI Discovery. |
| `AI_DISCOVERY_INTERVAL_SECONDS` | `600` | Automatic signal-only scan interval; minimum accepted value is 60 seconds. |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Local Ollama server URL. |
| `OLLAMA_MODEL` | `qwen3:8b` | Model name expected on the Ollama server. |
| `OLLAMA_NUM_THREADS` | `2` | CPU thread setting passed to Ollama. |
| `DEXSCREENER_API_URL` | `https://api.dexscreener.com` | DexScreener API base URL. |
| `HELIUS_API_KEYS` | Empty | Optional comma-separated Helius keys; current Helius integration is a scaffold. |
| `ENABLED_CHAINS` | `solana,robinhood_chain,base,bnb_chain,ethereum,monad` | Configured chain names. |

Avoid committing `.env` files or credentials. For local development, a useful minimal `.env` can be as simple as `FOMO_ACTIVITY_MONITOR_ENABLED=false`; database and AI defaults continue to apply.

## Database and Background Work

SQLAlchemy model definitions live in `app/models/records.py`. The main records are:

- `Chain` and `Token`: supported networks and tracked contract identity.
- `MarketObservation` and `DiscoveryEvent`: timestamped market data and discovery baselines.
- `FollowedTrader`, `TraderTransactionEvent`, and `TraderTokenPick`: profiles, observed buy events, and per-trader token performance.
- `Favorite`, `GraduationEvent`, `RiskFinding`, and `DismissedToken`: research actions and derived review outcomes.
- `AIIntelligenceRecord` and `BackgroundJob`: AI candidate runs, feedback/baselines/picks, and manual scan state.
- `ProviderSyncCheckpoint` and `AuditEvent`: provider/sync and audit-oriented records.

On startup, `app/main.py` creates missing tables, reconciles graduation eligibility, backfills qualifying trader picks, and starts the market-refresh and AI-discovery async loops. Market refresh starts immediately and repeats after its configured interval. The scheduled AI worker waits its interval before its first automatic signal-only scan. If browser monitoring is enabled, a third long-running monitor task polls Fomo Family profiles and metadata. Tasks are cancelled and the browser is stopped when the app shuts down.

Manual AI scans are queued through FastAPI's in-process background-task mechanism and tracked in the `background_jobs` table. They are not durable distributed jobs: stopping the server can stop in-flight work. Celery and Redis are declared/configured but are not wired into these current workflows.

## API and Tests

All feature routers are mounted under `/api/v1`. Useful endpoints include:

- `GET /api/v1/health`: health and Fomo monitor status.
- `GET /api/v1/tokens/` and `POST /api/v1/tokens/`: list or add tracked contracts.
- `GET /api/v1/followed-traders/` and `POST /api/v1/followed-traders/`: list or follow profiles.
- `GET /api/v1/ai-discovery/` and `POST /api/v1/ai-discovery/run`: inspect or run candidate analysis.
- `GET /api/v1/market-data/history`: inspect stored market observations.
- `/docs`: interactive OpenAPI/Swagger documentation.

Run all automated tests from the repository root:

```bash
python -m pytest -q
```

For a focused UI/API smoke test:

```bash
python -m pytest tests/test_root_ui.py -q
```

## Limitations and Safety Notes

- Collection depends on external provider availability, rate limits, and response formats. Missing values are common; a blank field means the provider did not supply usable data, not necessarily that the token has none.
- Fomo Family scraping depends on an authenticated browser session and the website's current API behavior. Respect the service's terms and applicable laws; the monitor is not a method to bypass access controls.
- DexScreener market data is an observation, not an execution price or guarantee that liquidity is available at that value.
- The Helius client and generic Fomo Family client are scaffolding; they do not currently provide the working collection paths described above.
- Risk findings are narrow rules (including observed market-cap drawdown), not a smart-contract audit or fraud verdict.
- AI scores and trader performance are research summaries based on available observations. They are not financial advice, price predictions, realized returns, or guarantees of future results.
- The dashboard has no user authentication configured. Do not expose it to an untrusted network without adding access control and reviewing deployment security.