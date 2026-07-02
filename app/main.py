import copy
import hashlib
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.data.providers import PROVIDERS, CATEGORIES, PROVIDER_COLORS
from app.data.geo import CITIES, DEFAULT_CITY, REGION_CLUSTERS
from app.database import (
    init_db,
    record_prices,
    get_price_baseline,
    get_price_history,
    save_override,
    get_overrides,
)
from app.models import PriceUpdate
from app.scoring import (
    compute_scores,
    is_open_weight,
    WORKLOAD_PRESETS,
    LATENCY_KNEE,
    DEFAULT_WORKLOAD,
    DEFAULT_SENSITIVITY,
)
from app.scraper import apply_cached_quality_indices, refresh_quality_indices

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


scheduler = AsyncIOScheduler()
_last_updated: str = ""
_data_version: int = 0           # bumped on every rebuild; ETag identity
                                 # (_last_updated is minute-granular, too
                                 # coarse to distinguish an override that
                                 # lands in the same minute as a refresh)
_raw_models: list = []           # with overrides + scraped indices, no scores
_scored_cache: dict = {}         # key: (workload, sensitivity, city) → scored list

# Minimum seconds between unauthenticated POSTs per route. The dashboard
# is single-user-ish today; this is just enough to stop a runaway loop
# from amplifying external API calls (refresh fetches AA + Arena).
_THROTTLE_SECONDS = {"refresh": 30.0, "update": 5.0}
_last_post_at: dict = {}


def _require_admin(request: Request) -> None:
    """Gate mutating endpoints behind a shared-secret header.

    Both POSTs alter global state (persisted price overrides, upstream
    API quota), so they can't stay open on a public deployment. When
    ADMIN_TOKEN isn't configured the endpoints are disabled outright
    rather than silently open.
    """
    expected = os.environ.get("ADMIN_TOKEN", "")
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="Admin endpoints disabled: ADMIN_TOKEN not configured",
        )
    provided = request.headers.get("x-admin-token", "")
    if not secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=403, detail="Invalid admin token")


def _throttle_or_raise(key: str) -> None:
    now = time.monotonic()
    last = _last_post_at.get(key, 0.0)
    wait = _THROTTLE_SECONDS[key] - (now - last)
    if wait > 0:
        raise HTTPException(
            status_code=429,
            detail=f"Too many requests; retry in {wait:.1f}s",
            headers={"Retry-After": str(int(wait) + 1)},
        )
    _last_post_at[key] = now


def _validated(value: Optional[str], allowed, default: str) -> str:
    return value if value in allowed else default


def _pct_change(old: Optional[float], new: float) -> Optional[float]:
    if old is None or old <= 0:
        return None
    return round((new - old) / old * 100, 1)


async def _refresh_models(fetch_live: bool = True):
    """Rebuild the in-memory catalogue: seeds + DB overrides + quality data.

    fetch_live=False reuses the scraper's cached AA/Arena data instead of
    hitting upstream — right for override saves, where only prices changed
    and an external refetch would burn AA quota for nothing.
    """
    global _raw_models, _last_updated, _data_version
    models = copy.deepcopy(PROVIDERS)

    overrides = await get_overrides()
    for m in models:
        if m["id"] in overrides:
            ov = overrides[m["id"]]
            m["input_usd_per_1m"] = ov["input_usd_per_1m"]
            m["output_usd_per_1m"] = ov["output_usd_per_1m"]
            if ov.get("notes"):
                m["notes"] = ov["notes"]

    if fetch_live:
        models, fetch_counts = await refresh_quality_indices(models)
    else:
        models, fetch_counts = apply_cached_quality_indices(models)

    try:
        baseline = await get_price_baseline(days=7)
    except Exception as e:
        logger.warning("Failed to load price baseline: %s", e)
        baseline = {}

    for m in models:
        base = baseline.get(m["id"])
        m["input_change_pct_7d"] = _pct_change(base and base["input_usd_per_1m"], m["input_usd_per_1m"])
        m["output_change_pct_7d"] = _pct_change(base and base["output_usd_per_1m"], m["output_usd_per_1m"])
        m["is_open_weight"] = is_open_weight(m)
        # `apply_live_scores` set aa_index_source="live" on every entry it
        # touched. Anything else with a non-null aa_index is using the
        # seeded fallback from providers.py — tag it so the UI can show
        # data provenance.
        if "aa_index_source" not in m:
            aa = m.get("aa_index")
            m["aa_index_source"] = "seeded" if aa else "none"

    _raw_models = models
    _scored_cache.clear()
    _data_version += 1
    _last_updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    try:
        await record_prices(models)
    except Exception as e:
        logger.warning("Failed to record price snapshot: %s", e)
    logger.info(
        "Models refreshed: %d entries (live: aa=%d aa_tps=%d arena=%d)",
        len(models), fetch_counts["aa"], fetch_counts["aa_tps"], fetch_counts["arena"],
    )


def _get_scored(workload: str, sensitivity: str, city: str) -> list:
    key = (workload, sensitivity, city)
    if key not in _scored_cache:
        models = copy.deepcopy(_raw_models)
        _scored_cache[key] = compute_scores(models, workload, sensitivity, city)
    return _scored_cache[key]


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await _refresh_models()
    scheduler.add_job(_refresh_models, "interval", hours=6, id="refresh")
    scheduler.start()
    try:
        yield
    finally:
        scheduler.shutdown()


app = FastAPI(title="LLM Price Runner", version="3.2.0", lifespan=lifespan)

app.add_middleware(GZipMiddleware, minimum_size=1024)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    # 'unsafe-inline' is required: the dashboard is a single template with
    # inline <style> and <script>. Everything else is same-origin.
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'",
    )
    response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if request.url.path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "public, max-age=86400")
    return response


def _etag_response(request: Request, payload: dict, *key_parts: str):
    """Wrap a JSON payload with a weak ETag derived from the data's
    refresh timestamp + query identity, honouring If-None-Match.

    The catalogue only changes on refresh (every 6 h) or an admin
    override — both bump `_last_updated` — so revalidation is a cheap
    304 nearly always.
    """
    etag = 'W/"' + hashlib.md5("|".join(key_parts).encode()).hexdigest() + '"'
    headers = {"ETag": etag, "Cache-Control": "public, max-age=300"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return JSONResponse(content=payload, headers=headers)


templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# ── HTML Dashboard ────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    # Server-render the default view's rows (frontier tier, default
    # workload/sensitivity/city, sorted by AI Value) so crawlers and
    # no-JS clients see real content; boot() replaces them client-side.
    models = _get_scored(DEFAULT_WORKLOAD, DEFAULT_SENSITIVITY, DEFAULT_CITY)
    initial = sorted(
        (m for m in models if m["quality_index"] >= 35),
        key=lambda m: m["ai_value_score"],
        reverse=True,
    )
    return templates.TemplateResponse("index.html", {
        "request": request,
        "last_updated": _last_updated,
        "initial_models": initial,
    })


@app.head("/")
async def dashboard_head():
    return HTMLResponse("")


# ── JSON API ──────────────────────────────────────────────────────────────────

@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.head("/healthz")
async def healthz_head():
    return Response()


@app.get("/robots.txt", response_class=PlainTextResponse)
async def robots():
    return "User-agent: *\nAllow: /\nDisallow: /api/\n"


@app.get("/api/models")
async def get_models(
    request: Request,
    category: Optional[str] = None,
    workload: Optional[str] = None,
    sensitivity: Optional[str] = None,
    city: Optional[str] = None,
):
    wl = _validated(workload, WORKLOAD_PRESETS, DEFAULT_WORKLOAD)
    sn = _validated(sensitivity, LATENCY_KNEE, DEFAULT_SENSITIVITY)
    ct = _validated(city, CITIES, DEFAULT_CITY)
    models = _get_scored(wl, sn, ct)
    if category and category != "all":
        models = [m for m in models if category in m["categories"]]
    payload = {
        "models": models,
        "last_updated": _last_updated,
        "total": len(models),
        "workload": wl,
        "sensitivity": sn,
        "city": ct,
    }
    return _etag_response(request, payload, wl, sn, ct, category or "all", str(_data_version))


@app.get("/api/history/{model_id}")
async def get_history(model_id: str):
    history = await get_price_history(model_id)
    return {"model_id": model_id, "history": history}


@app.post("/api/update")
async def update_price(update: PriceUpdate, request: Request):
    _require_admin(request)
    _throttle_or_raise("update")
    if update.id not in {m["id"] for m in PROVIDERS}:
        raise HTTPException(status_code=404, detail="Model not found")
    await save_override(update.id, update.input_usd_per_1m, update.output_usd_per_1m, update.notes or "")
    await _refresh_models(fetch_live=False)
    return {"status": "ok", "id": update.id}


@app.post("/api/refresh")
async def manual_refresh(request: Request):
    _require_admin(request)
    _throttle_or_raise("refresh")
    await _refresh_models()
    return {"status": "ok", "last_updated": _last_updated}


@app.get("/api/meta")
async def meta(request: Request):
    payload = {
        "categories": CATEGORIES,
        "provider_colors": PROVIDER_COLORS,
        "last_updated": _last_updated,
        "model_count": len(_raw_models),
        "provider_count": len({m["provider"] for m in _raw_models}),
        "workloads": [
            {"id": k, "label": k.capitalize(), "ratio": f"{v[0]}:{v[1]} in:out"}
            for k, v in WORKLOAD_PRESETS.items()
        ],
        "sensitivities": [
            {"id": k, "label": k.capitalize(), "knee_ms": v}
            for k, v in LATENCY_KNEE.items()
        ],
        "cities": [
            {"id": k, "label": v["label"], "flag": v["flag"], "cluster": v["cluster"]}
            for k, v in CITIES.items()
        ],
        "clusters": [
            {"id": k, "label": v["label"], "flag": v["flag"]}
            for k, v in REGION_CLUSTERS.items()
        ],
        "default_workload": DEFAULT_WORKLOAD,
        "default_sensitivity": DEFAULT_SENSITIVITY,
        "default_city": DEFAULT_CITY,
    }
    return _etag_response(request, payload, "meta", str(_data_version))
