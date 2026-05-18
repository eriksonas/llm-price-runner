"""
AI Value scoring — workload- and city-aware, absolute-unit methodology.

Core formula:
    ai_value = log10(intel_per_dollar) − latency_adjustment

Where:
    intel_per_dollar    = AA Intelligence Index / blended $/1M tokens (for chosen workload)
    scoring_latency     = ttft_ms + network_rtt_ms + α(workload) · generation_time_ms
    latency_adjustment  = log10((ms + knee/4) / (knee + knee/4))
                          — negative (bonus) when ms < knee
                          — zero at ms = knee
                          — positive (penalty) when ms > knee

Interpretation:
    Each +1.0 on ai_value ≈ 10× more intelligence per dollar (net of latency cost).
    No post-hoc rescaling — the number has a fixed economic meaning across refreshes.
    Low-latency models earn a bounded bonus (max ≈ +0.7 at zero scoring latency).

Latency components:
    ttft_ms        : time to first token (pure inference), derived from the
                     catalogue's Vilnius-measured eu_latency_ms minus the
                     estimated Vilnius→region RTT, floored at 20 ms.
    network_rtt_ms : RTT for the user's selected city → model's region —
                     prefers RIPE-Atlas-measured values, falls back to
                     haversine estimate (see app.data.geo).
    generation_time_ms : WORKLOAD_OUTPUT_TOKENS / throughput_tps · 1000.
                     Folded into scoring_latency via a workload-specific
                     α (WORKLOAD_LATENCY_ALPHA) so that streamed workloads
                     (chat α=0.3, code α=0.5) value throughput and
                     kicked-off workloads (rag/summarize α=0.05) don't.

`effective_latency_ms` (TTFT + RTT only) is still exposed for UI display —
it represents time-to-first-chunk. `scoring_latency_ms` is what the penalty
is computed from.

Embeddings use input-only cost (no output tokens) and compete only inside
their own category via best_per_category().
"""

import math

from app.data.geo import (
    CITIES,
    DEFAULT_CITY,
    canonical_region,
    estimate_rtt_ms,
)


# ── Workload presets: (input_tokens, output_tokens) ──────────────────────────

WORKLOAD_PRESETS = {
    "chat":      (1, 3),
    "rag":       (5, 1),
    "code":      (1, 5),
    "summarize": (10, 1),
    "balanced":  (1, 1),
}
DEFAULT_WORKLOAD = "chat"

# Typical output-token count per workload — used to estimate a realistic
# "typical response time" that combines TTFT, network RTT, and generation
# time. For chat/code the output side dominates total time, so throughput
# matters; for RAG/summarize it barely affects perceived latency.
WORKLOAD_OUTPUT_TOKENS = {
    "chat":      400,
    "rag":       200,
    "code":      800,
    "summarize": 300,
    "balanced":  300,
}


# ── Latency sensitivity: knee position (ms) of the log penalty ───────────────
#
# Knees sit where "responsive" ends for that mode — not below it. The previous
# 80 ms interactive knee was sub-achievable for most non-co-located users
# (TTFT floor ~20 ms + any realistic RTT), so nearly every model took a
# penalty and fast-EU models got only a trivial bonus. 150/500/2000 places
# each knee at a realistic boundary.
LATENCY_KNEE = {
    "interactive": 150,
    "batch":       500,
    "insensitive": 2000,
}
DEFAULT_SENSITIVITY = "interactive"

# How much of the output-generation time folds into the scoring latency.
# Chat/code are streamed and the user perceives total completion time, so
# throughput matters. RAG/summarize are often fire-and-check, so TTFT
# dominates perceived responsiveness.
WORKLOAD_LATENCY_ALPHA = {
    "chat":      0.3,
    "rag":       0.05,
    "code":      0.5,
    "summarize": 0.05,
    "balanced":  0.2,
}


# ── Core primitives ──────────────────────────────────────────────────────────

def blended_cost_per_1m(model: dict, workload: str) -> float:
    in_r, out_r = WORKLOAD_PRESETS.get(workload, WORKLOAD_PRESETS[DEFAULT_WORKLOAD])
    total = in_r + out_r
    cost = (in_r * model["input_usd_per_1m"] + out_r * model["output_usd_per_1m"]) / total

    if "embeddings" in model.get("categories", []) and model["output_usd_per_1m"] == 0:
        cost = model["input_usd_per_1m"]

    return cost


def quality_index_0_100(model: dict) -> float:
    """Return the model's quality index on the AA 2026 scale (top ~57).

    Falls back to a calibrated rescale of the hand-seeded `quality_score`
    (5.0–10.0 range) when AA hasn't indexed the model. The naive `× 10`
    fallback used to map a seeded 9.8 → 98, which sat ~2× above AA's
    actual frontier ceiling and let unindexed models silently dominate
    rankings. The calibrated map keeps the fallback strictly below the
    measured frontier so AA-indexed models always win on equal merit.
    """
    aa = model.get("aa_index")
    if aa and aa > 0:
        return float(aa)
    qs = float(model.get("quality_score", 7.0))
    return max(0.0, min(55.0, (qs - 4.0) * 9.0))


def latency_penalty(latency_ms: int, sensitivity: str) -> float:
    """Symmetric log adjustment around the sensitivity knee.

    Negative (bonus) when ms < knee, zero at ms = knee, positive above.
    The (knee/4) shift bounds the bonus to ≈ 0.7 at ms = 0 so near-zero
    latencies can't dominate; penalties remain unbounded as ms → ∞.
    """
    knee = LATENCY_KNEE.get(sensitivity, LATENCY_KNEE[DEFAULT_SENSITIVITY])
    shift = knee / 4
    ms = max(0, latency_ms)
    return math.log10((ms + shift) / (knee + shift))


# ── TTFT + throughput derivation ─────────────────────────────────────────────

def derive_ttft_ms(model: dict) -> int:
    """Pure inference time (time to first token) for this model.

    The catalogue stores eu_latency_ms as a rough "measured from Vilnius"
    number. We back out the predicted Vilnius→region network RTT to get
    the model's inference-only component, with a 20 ms floor so that
    aspirationally-low catalogue numbers don't go negative.
    """
    region = model.get("primary_region", "multi")
    base_rtt = estimate_rtt_ms(DEFAULT_CITY, region)
    raw = model.get("eu_latency_ms", 200) - base_rtt
    return max(20, raw)


def derive_throughput_tps(model: dict) -> int:
    """Output tokens per second.

    Prefers the measured value from Artificial Analysis (`aa_tps`) when the
    scraper attached one — that's per-endpoint median throughput, much more
    honest than the class-level prior. Falls back to a category- and
    quality-bucketed prior otherwise so unindexed models still rank
    sensibly.
    """
    cats = model.get("categories", [])
    if "embeddings" in cats:
        return 0  # not applicable

    measured = model.get("aa_tps")
    if measured and measured > 0:
        return int(measured)

    aa = model.get("aa_index") or (model.get("quality_score", 7.0) * 10.0)

    if "reasoning" in cats:
        # Reasoning models stream a lot of tokens but do so slowly;
        # plus chain-of-thought inflates effective tokens per answer.
        if aa >= 90:
            return 25
        if aa >= 80:
            return 40
        return 55

    if aa >= 88:
        return 45
    if aa >= 75:
        return 75
    if aa >= 60:
        return 120
    return 180


# ── Per-model scoring with full audit trail ──────────────────────────────────

def compute_model_score(model: dict, workload: str, sensitivity: str, city: str) -> dict:
    if city not in CITIES:
        city = DEFAULT_CITY

    q = quality_index_0_100(model)
    cost = blended_cost_per_1m(model, workload)
    effective_cost = max(cost, 0.001)
    intel_per_dollar = q / effective_cost

    ttft = derive_ttft_ms(model)
    rtt = estimate_rtt_ms(city, model.get("primary_region", "multi"))
    effective_latency_ms = ttft + rtt

    tps = derive_throughput_tps(model)
    throughput_source = "aa-measured" if model.get("aa_tps") else (
        "class-prior" if tps > 0 else "n/a"
    )
    out_tokens = WORKLOAD_OUTPUT_TOKENS.get(workload, 300)
    gen_time_ms = int(round(out_tokens / tps * 1000)) if tps > 0 else 0
    typical_response_ms = effective_latency_ms + gen_time_ms

    alpha = WORKLOAD_LATENCY_ALPHA.get(workload, 0.2)
    gen_latency_contribution_ms = int(round(alpha * gen_time_ms))
    scoring_latency_ms = effective_latency_ms + gen_latency_contribution_ms

    lat_pen = latency_penalty(scoring_latency_ms, sensitivity)
    ai_value = math.log10(max(intel_per_dollar, 0.01)) - lat_pen

    in_r, out_r = WORKLOAD_PRESETS.get(workload, WORKLOAD_PRESETS[DEFAULT_WORKLOAD])

    return {
        "quality_index":           round(q, 1),
        "blended_cost_usd_per_1m": round(cost, 4),
        "intel_per_dollar":        round(intel_per_dollar, 2),

        # Latency, split
        "ttft_ms":                 int(ttft),
        "network_rtt_ms":          int(rtt),
        "effective_latency_ms":    int(effective_latency_ms),
        "output_tokens_per_sec":   int(tps),
        "throughput_source":       throughput_source,
        "typical_response_ms":     int(typical_response_ms),
        "gen_latency_contribution_ms": int(gen_latency_contribution_ms),
        "scoring_latency_ms":      int(scoring_latency_ms),
        "canonical_region":        canonical_region(model.get("primary_region", "multi")),

        "latency_penalty":         round(lat_pen, 3),
        "ai_value_score":          round(ai_value, 3),
        "workload_ratio":          f"{in_r}:{out_r}",
        "latency_knee_ms":         LATENCY_KNEE.get(sensitivity, LATENCY_KNEE[DEFAULT_SENSITIVITY]),
        "latency_alpha":           WORKLOAD_LATENCY_ALPHA.get(workload, 0.2),
    }


def compute_scores(models: list,
                   workload: str = DEFAULT_WORKLOAD,
                   sensitivity: str = DEFAULT_SENSITIVITY,
                   city: str = DEFAULT_CITY) -> list:
    for m in models:
        breakdown = compute_model_score(m, workload, sensitivity, city)
        m.update(breakdown)

        m["_workload"] = workload
        m["_sensitivity"] = sensitivity
        m["_city"] = city

        # The UI's "Latency" column follows the city picker. Expose that as
        # a dedicated field so the catalogue's Vilnius-baseline value
        # (`eu_latency_ms`) stays intact and readable in API responses.
        m["display_latency_ms"] = m["effective_latency_ms"]

        # Backwards-compatible fields for the existing table UI.
        m["value_score"] = m["ai_value_score"]
        m["quality_composite"] = round(m["quality_index"] / 10.0, 2)
        m["latency_factor"] = round(10 ** (-m["latency_penalty"]), 2)
    return models


def best_per_category(models: list, categories: list) -> dict:
    result = {}
    for cat in categories:
        if cat["id"] == "all":
            continue
        eligible = [m for m in models if cat["id"] in m["categories"]]
        if eligible:
            result[cat["id"]] = max(eligible, key=lambda m: m["ai_value_score"])
    return result


# ── License classification ───────────────────────────────────────────────────
#
# Open-weight = model weights are publicly downloadable (Apache/MIT/Llama
# Community License/etc), regardless of who's serving the API. The provider
# alone is usually decisive — pure-aggregator hosts have to be classified
# per-model. Kept here so the rule lives in one place and the API exposes
# `is_open_weight` directly; the dashboard JS just reads the flag.

_OPEN_PROVIDERS = {
    "Meta (API)", "DeepSeek", "Qwen", "Moonshot AI", "Z.ai",
    "Groq", "Cerebras", "Together AI", "Fireworks AI", "SiliconFlow", "NVIDIA",
}
_PROP_PROVIDERS = {
    "OpenAI", "Anthropic", "Cohere", "xAI", "Azure OpenAI",
    "Perplexity", "ByteDance", "Baidu", "AI21 Labs",
}

import re as _re

_OPEN_ID_PATTERNS = {
    "Google":      _re.compile(r"gemma", _re.I),
    "Aleph Alpha": _re.compile(r"pharia", _re.I),
    "AWS Bedrock": _re.compile(r"llama|mistral", _re.I),
    "OpenRouter":  _re.compile(r"llama|deepseek|kimi|qwen|mixtral|gemma|glm", _re.I),
    "Replicate":   _re.compile(r"deepseek|llama|mistral", _re.I),
}
# Mistral hosts both open weights and proprietary mediums/larges from the
# same provider name — invert the test so "everything except these" is open.
_MISTRAL_PROP_PAT = _re.compile(
    r"mistral-large|mistral-medium|magistral-medium|pixtral-large|codestral", _re.I
)


def is_open_weight(model: dict) -> bool:
    provider = model.get("provider", "")
    if provider in _OPEN_PROVIDERS:
        return True
    if provider in _PROP_PROVIDERS:
        return False
    mid = model.get("model_id", "") or ""
    if provider == "Mistral AI":
        return not _MISTRAL_PROP_PAT.search(mid)
    pat = _OPEN_ID_PATTERNS.get(provider)
    return bool(pat and pat.search(mid))
