import copy
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.data.providers import PROVIDERS, CATEGORIES, PROVIDER_COLORS
from app.data.geo import CITIES, DEFAULT_CITY, REGION_CLUSTERS
from app.database import init_db, record_prices, get_price_history, save_override, get_overrides
from app.models import PriceUpdate
from app.scoring import (
    compute_scores,
    is_open_weight,
    WORKLOAD_PRESETS,
    LATENCY_KNEE,
    DEFAULT_WORKLOAD,
    DEFAULT_SENSITIVITY,
)
from app.scraper import refresh_quality_indices

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


scheduler = AsyncIOScheduler()
_last_updated: str = ""
_raw_models: list = []           # with overrides + scraped indices, no scores
_scored_cache: dict = {}         # key: (workload, sensitivity, city) → scored list

# Minimum seconds between unauthenticated POSTs per route. The dashboard
# is single-user-ish today; this is just enough to stop a runaway loop
# from amplifying external API calls (refresh fetches AA + Arena).
_THROTTLE_SECONDS = {"refresh": 30.0, "update": 5.0}
_last_post_at: dict = {}


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


async def _refresh_models():
    global _raw_models, _last_updated
    models = copy.deepcopy(PROVIDERS)

    overrides = await get_overrides()
    for m in models:
        if m["id"] in overrides:
            ov = overrides[m["id"]]
            m["input_usd_per_1m"] = ov["input_usd_per_1m"]
            m["output_usd_per_1m"] = ov["output_usd_per_1m"]
            if ov.get("notes"):
                m["notes"] = ov["notes"]

    models = await refresh_quality_indices(models)

    for m in models:
        m["is_open_weight"] = is_open_weight(m)

    _raw_models = models
    _scored_cache.clear()
    _last_updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    try:
        await record_prices(models)
    except Exception as e:
        logger.warning("Failed to record price snapshot: %s", e)
    logger.info("Models refreshed: %d entries", len(models))


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


app = FastAPI(title="LLM Price Runner", version="3.1.0", lifespan=lifespan)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# ── HTML Dashboard ────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("index.html", {
        "request": request,
        "last_updated": _last_updated,
    })


# ── JSON API ──────────────────────────────────────────────────────────────────

@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/api/models")
async def get_models(
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
    return {
        "models": models,
        "last_updated": _last_updated,
        "total": len(models),
        "workload": wl,
        "sensitivity": sn,
        "city": ct,
    }


@app.get("/api/history/{model_id}")
async def get_history(model_id: str):
    history = await get_price_history(model_id)
    return {"model_id": model_id, "history": history}


@app.post("/api/update")
async def update_price(update: PriceUpdate):
    _throttle_or_raise("update")
    if update.id not in {m["id"] for m in PROVIDERS}:
        raise HTTPException(status_code=404, detail="Model not found")
    await save_override(update.id, update.input_usd_per_1m, update.output_usd_per_1m, update.notes or "")
    await _refresh_models()
    return {"status": "ok", "id": update.id}


@app.post("/api/refresh")
async def manual_refresh():
    _throttle_or_raise("refresh")
    await _refresh_models()
    return {"status": "ok", "last_updated": _last_updated}


@app.get("/api/meta")
async def meta():
    return {
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
