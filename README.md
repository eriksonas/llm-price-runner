# LLM Price Runner

A FastAPI dashboard that ranks ~200 large-language-model endpoints by an
absolute **AI Value** score — intelligence per dollar adjusted for the
latency you'd actually see from your city, refreshed every 6 hours from
Artificial Analysis and LMSYS Arena.

Live instance: <https://models.agent-startup.com>

```
AI Value = log10(quality_index / blended_cost_per_1M)
         − log10((scoring_latency_ms + knee/4) / (knee + knee/4))
```

A +1.0 on AI Value means roughly 10× more intelligence per dollar (net of
latency cost). Scores are absolute and comparable across refreshes — no
post-hoc rescaling.

## Quick start

### Docker (recommended)

```bash
docker compose up -d --build
# dashboard at http://localhost:8080
```

The compose file expects an external Traefik network for HTTPS termination
in production; remove the `traefik-proxy` network and the labels block for
plain local use.

### Local dev

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8080
```

SQLite lives at `/data/pricerunner.db` by default — change `DB_PATH` in
`app/database.py` for local runs.

## What it does

- **~200 LLMs across ~25 providers** seeded in `app/data/providers.py`
  with input/output prices, primary region, AA Intelligence Index, and
  Arena ELO.
- **Live refresh every 6 h** from `artificialanalysis.ai/api/models` and
  `lmarena.ai/api/leaderboard`. Failures fall back to seeded values.
- **Workload presets** (chat / RAG / code / summarize / balanced) shift
  the input:output cost blend.
- **Latency sensitivity** knees (interactive 150 ms / batch 500 ms /
  insensitive 2000 ms) tune how harshly slow models are penalised.
- **City-aware RTT**: 27 cities × 8 canonical regions. Measured RTTs from
  RIPE Atlas live in `app/data/rtt_measured.json` (refreshed out-of-band
  by `tools/run_probes.py`); missing pairs fall back to a haversine
  estimate.
- **Price-history charting** per model in the modal — overrides via
  `POST /api/update` are persisted in SQLite and survive restarts.
- **Open-weight vs proprietary** classification is computed server-side
  from provider + model-id heuristics in `app/scoring.py`.

## API

| Endpoint | Description |
|---|---|
| `GET /` | Dashboard UI |
| `GET /healthz` | `{"ok": true}` — used by the Docker healthcheck |
| `GET /api/meta` | Categories, providers, workloads, sensitivities, cities |
| `GET /api/models?workload=chat&sensitivity=interactive&city=vilnius&category=code` | Scored models, filterable |
| `GET /api/history/{id}` | Daily price snapshots for a model |
| `POST /api/update` | Manual price override (throttled: 5 s / call) |
| `POST /api/refresh` | Force a refresh from upstream leaderboards (throttled: 30 s / call) |

`POST /api/update` payload:

```json
{
  "id": "openai-gpt-4o",
  "input_usd_per_1m": 2.50,
  "output_usd_per_1m": 10.00,
  "notes": "verified 2026-05-18"
}
```

`id` is the catalogue slug from `app/data/providers.py` (e.g.
`openai-gpt-4o`), **not** the provider's API model_id (`gpt-4o`).

## Scoring methodology

Each model is scored against a chosen `(workload, sensitivity, city)`:

1. **Quality** — Artificial Analysis Intelligence Index (AA 2026 scale,
   frontier ~57). Falls back to a calibrated rescale of a hand-seeded
   `quality_score` when AA hasn't indexed the model — and the fallback is
   capped at 55 so unindexed models can't outrank measured frontier ones.
2. **Cost** — workload-weighted blend of input and output $/1M tokens.
3. **Effective latency** — `ttft_ms + rtt_ms`. TTFT is backed out of the
   catalogue's Vilnius-measured `eu_latency_ms` field; RTT prefers RIPE
   Atlas measurements over haversine when available.
4. **Throughput** — preferred from AA's measured median when present,
   otherwise a category- and quality-bucketed prior.
5. **Scoring latency** — `effective_latency_ms + α(workload) ·
   generation_time_ms`. α is 0.05–0.5 so streamed chat/code workloads
   value throughput while batch RAG/summarize barely do.
6. **Latency adjustment** — symmetric `log10((ms + knee/4) / (knee +
   knee/4))`. Negative bonus below the knee (bounded to ≈ −0.7), zero at
   the knee, unbounded penalty above.

See `app/scoring.py` for the full implementation and inline derivation.

## Tests

```bash
pip install -r requirements-dev.txt
pytest tests/
```

29 tests pin the scoring formula against four representative models
across four workload × sensitivity × city combinations, plus focused
unit tests for open-weight classification, the AA quality fallback cap,
the Arena ELO matcher, and latency-penalty symmetry around the knee.

When the formula intentionally changes, regenerate the `EXPECTED` dict
at the top of `tests/test_scoring.py` and review the diff.

## RIPE Atlas probes (optional)

`tools/run_probes.py` measures real-world RTT from probes near each user
city to each canonical region hub, then writes medians to
`app/data/rtt_measured.json`. The running app picks up changes via an
mtime check — no restart needed.

```bash
export RIPE_ATLAS_KEY=...                  # measurement-create scope
python -m tools.run_probes --dry            # plan only, estimates credits
python -m tools.run_probes                  # full refresh
python -m tools.run_probes --city vilnius   # single city
```

A run for all 27 cities × 8 regions costs ~3 000 RIPE Atlas credits.

## Deployment

See [DEPLOY.md](./DEPLOY.md) for the Hostinger VPS + Traefik recipe with
Let's Encrypt.

The Docker image runs as an unprivileged `pricerunner` user and pins
uvicorn to a single worker — APScheduler runs in-process, so multiple
workers would each run the 6 h refresh and clobber SQLite. To scale
horizontally, move scheduling to a sidecar or out-of-process queue.

## Project structure

```
app/
  main.py             FastAPI app, lifespan, refresh loop, JSON API
  scoring.py          AI Value formula, open-weight classification
  scraper.py          AA Intelligence Index + Arena ELO fetchers
  database.py         SQLite overrides + price history
  models.py           Pydantic schemas
  data/
    providers.py      Seeded catalogue (~200 entries)
    geo.py            Cities, regions, RTT estimation
    rtt_measured.json RIPE Atlas measurements
  templates/index.html  Single-page dashboard
tools/
  run_probes.py       RIPE Atlas measurement runner
  migrate_aa_index.py AA slug migration helper
  update_arena_elo.py Arena ELO seed updater
tests/
  test_scoring.py     Snapshot + unit tests
```

## License

Not yet licensed. Until a `LICENSE` file is added, default copyright
applies — no rights granted. Open an issue if you'd like to use this.
