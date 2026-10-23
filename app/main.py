from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.schemas.system import ConfigResponse
from app.database import Base, SessionLocal, engine
from app.features.dashboard.routes import router as dashboard_router
from app.features.discovery.routes import router as discovery_router
from app.features.favorites.routes import router as favorites_router
from app.features.followed_users.routes import router as followed_users_router
from app.features.followed_users.performance import backfill_trader_token_picks
from app.features.graduated_tokens.routes import router as graduated_tokens_router
from app.features.graduated_tokens.service import reconcile_graduation_eligibility
from app.features.market_data.routes import router as market_data_router
from app.features.risk_analysis.routes import router as risk_router
from app.features.ai_discovery.routes import router as ai_discovery_router
from app.features.dismissed_tokens.routes import router as dismissed_tokens_router
from app.features.tokens.routes import router as tokens_router
from app.models import MarketObservation

settings = get_settings()
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        reconcile_graduation_eligibility(db, settings.graduation_threshold_usd)
        backfill_trader_token_picks(db)
    for index in MarketObservation.__table__.indexes:
        if index.name == "ix_market_observations_token_observed":
            index.create(bind=engine, checkfirst=True)
            break
    market_refresh_task = asyncio.create_task(
        _run_market_refresh(),
        name="market-data-refresh",
    )
    ai_discovery_task = asyncio.create_task(
        _run_ai_discovery(),
        name="ai-discovery-refresh",
    )
    monitor = None
    app.state.fomo_activity_monitor = None
    if settings.fomo_activity_monitor_enabled:
        from app.integrations.fomo_family.monitor import FomoFamilyActivityMonitor

        monitor = FomoFamilyActivityMonitor(
            cdp_url=settings.fomo_activity_cdp_url,
            browser_profile_dir=settings.fomo_activity_browser_profile_dir,
            profile_handle=settings.fomo_family_profile_handle,
            profile_url=settings.fomo_family_profile_url,
            following_sync_interval_seconds=settings.fomo_following_sync_interval_seconds,
            poll_interval_seconds=settings.fomo_activity_poll_interval_seconds,
            batch_size=settings.fomo_activity_batch_size,
            page_timeout_seconds=settings.fomo_activity_page_timeout_seconds,
        )
        app.state.fomo_activity_monitor = monitor
        monitor.start()
    try:
        yield
    finally:
        market_refresh_task.cancel()
        ai_discovery_task.cancel()
        await asyncio.gather(market_refresh_task, ai_discovery_task, return_exceptions=True)
        if monitor is not None:
            await monitor.stop()


async def _run_market_refresh() -> None:
    from app.workers.market_refresh import run_market_refresh

    await run_market_refresh(settings.token_refresh_interval_seconds, settings.dexscreener_api_url)


async def _run_ai_discovery() -> None:
    from app.workers.ai_discovery import run_ai_discovery

    await run_ai_discovery(settings.ai_discovery_interval_seconds)


app = FastAPI(
    title=settings.project_name,
    version="0.1.0",
    description="Multi-chain meme token intelligence platform",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(dashboard_router, prefix="/api/v1")
app.include_router(discovery_router, prefix="/api/v1")
app.include_router(followed_users_router, prefix="/api/v1")
app.include_router(market_data_router, prefix="/api/v1")
app.include_router(favorites_router, prefix="/api/v1")
app.include_router(graduated_tokens_router, prefix="/api/v1")
app.include_router(dismissed_tokens_router, prefix="/api/v1")
app.include_router(risk_router, prefix="/api/v1")
app.include_router(ai_discovery_router, prefix="/api/v1")
app.include_router(tokens_router, prefix="/api/v1")

app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

@app.get("/", response_class=FileResponse)
async def root() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/legacy", response_class=HTMLResponse)
async def legacy_root() -> str:
    return """
    <!doctype html>
    <html lang="en">
    <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>FomoProfleTracker</title>
        <style>
            :root {
                --bg: #040b16;
                --bg-2: #0a1322;
                --panel: rgba(12, 17, 27, 0.88);
                --panel-strong: rgba(15, 23, 42, 0.96);
                --text: #ecf3ff;
                --muted: #9aa9bf;
                --line: rgba(148, 163, 184, 0.18);
                --accent: #7c3aed;
                --accent-2: #35d399;
                --shadow: rgba(2, 6, 23, 0.5);
            }
            * { box-sizing: border-box; }
            html, body { margin: 0; padding: 0; }
            body {
                font-family: Inter, ui-sans-serif, system-ui, -apple-system, sans-serif;
                background: radial-gradient(circle at top left, rgba(124,58,237,0.22), transparent 22%), radial-gradient(circle at top right, rgba(52,211,153,0.12), transparent 20%), linear-gradient(135deg, #020817, var(--bg-2));
                color: var(--text);
            }
            .wrap { max-width: 1240px; margin: 26px auto; padding: 20px; }
            .topbar {
                display: flex; align-items: center; justify-content: space-between; gap: 14px; flex-wrap: wrap;
                background: rgba(15, 23, 42, 0.96); border: 1px solid var(--line); border-radius: 18px; padding: 18px 20px;
                box-shadow: 0 20px 40px var(--shadow);
            }
            .brand { display: flex; align-items: center; gap: 14px; }
            .brand-mark {
                width: 14px; height: 14px; border-radius: 50%; background: linear-gradient(135deg, var(--accent), #22d3ee);
                box-shadow: 0 0 18px rgba(124,58,237,0.7);
            }
            .title { font-weight: 800; letter-spacing: -0.05em; font-size: clamp(1.8rem, 2vw, 2.7rem); }
            .subtitle { color: var(--muted); font-size: 0.9rem; }
            .pill {
                border-radius: 999px; padding: 8px 12px; border: 1px solid rgba(52,211,153,0.35); background: rgba(52,211,153,0.08); color: #d1fae5; font-size: 0.75rem; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase;
            }
            .toolbar {
                display: flex; justify-content: space-between; align-items: center; gap: 14px; flex-wrap: wrap; margin-top: 18px;
            }
            .tabs { display: flex; flex-wrap: wrap; gap: 10px; }
            .tab {
                background: rgba(15, 23, 42, 0.78); border: 1px solid var(--line); border-radius: 11px; color: var(--text);
                padding: 10px 14px; cursor: pointer; font-weight: 700; transition: 0.2s ease;
            }
            .tab:hover { transform: translateY(-1px); }
            .tab.active {
                background: rgba(124, 58, 237, 0.12); border-color: rgba(124, 58, 237, 0.7); box-shadow: inset 0 0 0 1px rgba(124, 58, 237, 0.3);
            }
            .cta {
                border: none; background: linear-gradient(135deg, var(--accent), #4f46e5); color: white; border-radius: 12px; padding: 11px 16px; cursor: pointer; font-weight: 800; box-shadow: 0 12px 24px rgba(79,70,229,0.28);
            }
            .cards {
                display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-top: 18px;
            }
            .card {
                background: var(--panel); border: 1px solid var(--line); border-radius: 16px; padding: 18px; min-height: 120px;
            }
            .label { color: var(--muted); font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.08em; }
            .value { margin-top: 12px; font-weight: 800; font-size: 2rem; }
            .panel {
                margin-top: 18px; background: rgba(15, 23, 42, 0.9); border: 1px solid var(--line); border-radius: 18px; padding: 20px;
                box-shadow: 0 18px 40px rgba(2, 6, 23, 0.35);
            }
            .panel-header {
                display: flex; justify-content: space-between; align-items: center; gap: 8px; margin-bottom: 12px;
            }
            .panel-header h3 {
                margin: 0; font-size: 1.2rem; font-weight: 700;
            }
            .meta {
                background: rgba(2, 6, 23, 0.8); border-radius: 12px; border: 1px solid var(--line); padding: 14px; min-height: 220px; white-space: pre-wrap; font-family: 'SFMono-Regular', Consolas, monospace; color: #dbeafe; overflow: auto;
            }
            .note {
                margin-top: 10px; color: var(--muted); font-size: 0.93rem;
            }
            @media (max-width: 720px) {
                .wrap { padding: 12px; }
                .title { font-size: 1.7rem; }
                .toolbar { align-items: stretch; }
                .tabs, .cta { width: 100%; }
                .tabs { justify-content: flex-start; }
                .cta { justify-content: center; }
            }
        </style>
    </head>
    <body>
        <div class="wrap">
            <div class="topbar">
                <div class="brand">
                    <div class="brand-mark"></div>
                    <div>
                        <div class="title">FomoProfleTracker</div>
                        <div class="subtitle">Multi-chain meme-token intelligence</div>
                    </div>
                </div>
                <div class="pill">Operational</div>
            </div>

            <div class="toolbar">
                <div class="tabs">
                    <button class="tab active" data-tab="overview">Overview</button>
                    <button class="tab" data-tab="followed">Followed Traders</button>
                    <button class="tab" data-tab="graduated">Graduated</button>
                    <button class="tab" data-tab="risk">Risk</button>
                    <button class="tab" data-tab="ai">AI Discovery</button>
                </div>
                <button class="cta" id="sample-token">Test example meme token</button>
            </div>

            <div class="cards">
                <div class="card"><div class="label">Status</div><div id="card-status" class="value">--</div></div>
                <div class="card"><div class="label">Environment</div><div id="card-env" class="value">--</div></div>
                <div class="card"><div class="label">Refresh</div><div id="card-refresh" class="value">--</div></div>
            </div>

            <div class="panel">
                <div class="panel-header">
                    <h3 id="panel-title">Overview</h3>
                </div>
                <div id="panel-content" class="meta">Loading dashboard data...</div>
                <div id="panel-note" class="note"></div>
            </div>
        </div>

        <script>
            const tabButtons = Array.from(document.querySelectorAll('.tab'));
            const panelTitle = document.getElementById('panel-title');
            const panelContent = document.getElementById('panel-content');
            const panelNote = document.getElementById('panel-note');

            const tabMap = {
              overview: '/api/v1/dashboard/overview',
              followed: '/api/v1/followed-traders/',
              graduated: '/api/v1/graduated-tokens/',
              risk: '/api/v1/risk-analysis/',
              ai: '/api/v1/ai-discovery/'
            };

            async function fetchJson(url, options = {}) {
              const res = await fetch(url, { cache: 'no-store', ...options });
              if (!res.ok) {
                const txt = await res.text();
                throw new Error(`${res.status}: ${txt || 'request failed'}`);
              }
              return res.json();
            }

            function fallbackFor(name) {
              const map = {
                overview: { status: 'ok', note: 'No tracked tokens yet. Add a sample token to begin monitoring.' },
                followed: { status: 'manual_only', note: 'Followed traders remain manual-only until a valid Fomo Family token is configured.' },
                graduated: { status: 'empty', note: 'No graduated tokens yet.' },
                risk: { status: 'empty', note: 'No risk findings yet.' },
                ai: { status: 'disabled', note: 'AI discovery is intentionally disabled in this build.' }
              };
              return map[name] || { status: 'empty', note: 'No data available yet.' };
            }

            async function loadTab(name) {
              panelTitle.textContent = name.charAt(0).toUpperCase() + name.slice(1);
              panelContent.textContent = 'Loading...';
              panelNote.textContent = '';
              try {
                const data = await fetchJson(tabMap[name]);
                panelContent.textContent = JSON.stringify(data, null, 2);
                panelNote.textContent = data?.note || 'Data loaded successfully.';
              } catch (error) {
                const fallback = fallbackFor(name);
                panelContent.textContent = JSON.stringify(fallback, null, 2);
                panelNote.textContent = fallback.note || String(error);
              }
            }

            tabButtons.forEach((tab) => {
              tab.addEventListener('click', () => {
                tabButtons.forEach((btn) => btn.classList.toggle('active', btn === tab));
                loadTab(tab.dataset.tab);
              });
            });

            async function loadSummary() {
              try {
                const health = await fetchJson('/api/v1/health');
                const config = await fetchJson('/api/v1/config');
                document.getElementById('card-status').textContent = health.status || 'ok';
                document.getElementById('card-env').textContent = config.enabled_chains?.length ? 'development' : 'production';
                document.getElementById('card-refresh').textContent = `${config.token_refresh_interval_seconds || 60}s`;
              } catch (error) {
                document.getElementById('card-status').textContent = 'error';
                document.getElementById('card-env').textContent = 'n/a';
                document.getElementById('card-refresh').textContent = 'n/a';
              }
            }

            document.getElementById('sample-token').addEventListener('click', async () => {
              const sample = { chain_name: 'solana', address: 'So11111111111111111111111111111111111111112' };
              try {
                const result = await fetchJson(`/api/v1/tokens/?chain_name=${encodeURIComponent(sample.chain_name)}&address=${encodeURIComponent(sample.address)}`, { method: 'POST' });
                panelTitle.textContent = 'Overview';
                panelContent.textContent = JSON.stringify({ sampleToken: sample, result }, null, 2);
                panelNote.textContent = 'Example token added successfully. You can switch tabs to inspect the token state.';
                tabButtons.forEach((btn) => btn.classList.toggle('active', btn.dataset.tab === 'overview'));
              } catch (error) {
                panelTitle.textContent = 'Overview';
                panelContent.textContent = JSON.stringify({ error: String(error) }, null, 2);
                panelNote.textContent = 'The backend is reachable but the token request failed.';
              }
            });

            loadSummary();
            loadTab('overview');
        </script>
    </body>
    </html>
    """


def _fomo_activity_monitor_status(request: Request) -> dict:
    monitor = getattr(request.app.state, "fomo_activity_monitor", None)
    monitor_status = {
        "enabled": settings.fomo_activity_monitor_enabled,
        "state": "starting" if settings.fomo_activity_monitor_enabled else "disabled",
        "message": None,
    }
    if monitor is not None:
        monitor_status.update(monitor.status_snapshot())
    return monitor_status


@app.get("/health")
async def health(request: Request) -> dict:
    return {
        "status": "ok",
        "app": settings.app_name,
        "environment": settings.app_env,
        "token_refresh_interval_seconds": settings.token_refresh_interval_seconds,
        "fomo_activity_monitor": _fomo_activity_monitor_status(request),
    }


@app.get("/api/v1/health")
async def health_api(request: Request) -> dict:
    return {
        "status": "ok",
        "fomo_activity_monitor": _fomo_activity_monitor_status(request),
        "providers": {
            "fomo_family": "configured",
            "dexscreener": "configured",
            "helius": "configurable",
        },
        "database": "ready",
    }


@app.get("/api/v1/config", response_model=ConfigResponse)
async def config_api() -> ConfigResponse:
    return ConfigResponse(
        token_refresh_interval_seconds=settings.token_refresh_interval_seconds,
        discovery_threshold_usd=settings.discovery_threshold_usd,
        discovery_preferred_min_usd=settings.discovery_preferred_min_usd,
        discovery_preferred_max_usd=settings.discovery_preferred_max_usd,
        graduation_threshold_usd=settings.graduation_threshold_usd,
        ai_discovery_enabled=settings.ai_discovery_enabled,
        enabled_chains=settings.enabled_chain_list,
    )
