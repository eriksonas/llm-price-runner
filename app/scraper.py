"""
Fetches quality index data from public leaderboards:
  - Artificial Analysis Intelligence Index v4 (artificialanalysis.ai)
      Path:    /api/v2/data/llms/models
      Auth:    x-api-key header — free tier, sign up at
               https://artificialanalysis.ai (1000 req/day, ample headroom
               for our 4-per-day refresh schedule). Set AA_API_KEY env var.
      Shape:   data[].id / .slug, .evaluations.artificial_analysis_
               intelligence_index, .median_output_tokens_per_second.
  - LMSYS Chatbot Arena ELO (rebranded to LMArena Jan 2026, public API
    closed). We pull from a community-maintained daily snapshot:
      Path:    https://api.wulong.dev/arena-ai-leaderboards/v1/leaderboard
      Auth:    none
      Source:  https://github.com/oolong-tea-2026/arena-ai-leaderboards
      Shape:   models[].model (slug), .score (ELO), .vendor, .license.

Falls back to seeded values in providers.py if either fetch fails.
"""

import logging
import os

import httpx

logger = logging.getLogger(__name__)

AA_API_URL = "https://artificialanalysis.ai/api/v2/data/llms/models"
ARENA_API_URL = "https://api.wulong.dev/arena-ai-leaderboards/v1/leaderboard?name=text"

# ── Model ID mappings ─────────────────────────────────────────────────────────
# Maps our internal model_id → slug used by each leaderboard

AA_SLUGS = {
    # ── OpenAI ───────────────────────────────────────────────────────────────
    "gpt-5.4":                                       "gpt-5-4",
    "gpt-5.4-mini":                                  "gpt-5-4-mini",
    "gpt-5.4-nano":                                  "gpt-5-4-nano",
    "gpt-5.3-codex":                                 "gpt-5-3-codex",
    "gpt-5":                                         "gpt-5",
    "gpt-5-mini":                                    "gpt-5-mini",
    "gpt-5-nano":                                    "gpt-5-nano",
    "gpt-4.1":                                       "gpt-4-1",
    "gpt-4.1-mini":                                  "gpt-4-1-mini",
    "gpt-4.1-nano":                                  "gpt-4-1-nano",
    "gpt-4o":                                        "gpt-4o",
    "gpt-4o-mini":                                   "gpt-4o-mini",
    "o1":                                            "o1",
    "o1-mini":                                       "o1-mini",
    "o3":                                            "o3",
    "o3-mini":                                       "o3-mini",
    "o3-pro":                                        "o3-pro",
    "o4-mini":                                       "o4-mini",
    # gpt-5.4-pro, gpt-5.3-chat-latest, gpt-5-pro, o3-deep-research,
    # o4-mini-deep-research: not (yet) in AA's v4 catalogue — seeded
    # quality_index stays as fallback.

    # ── Anthropic ────────────────────────────────────────────────────────────
    "claude-opus-4-7":                               "claude-opus-4-7",
    "claude-opus-4-5":                               "claude-opus-4-5",
    "claude-sonnet-4-6":                             "claude-sonnet-4-6",
    "claude-sonnet-4-5":                             "claude-4-5-sonnet",
    "claude-3-7-sonnet-20250219":                    "claude-3-7-sonnet",
    "claude-3-5-sonnet-20241022":                    "claude-35-sonnet",
    "claude-haiku-4-5":                              "claude-4-5-haiku",
    "claude-3-5-haiku-20241022":                     "claude-3-5-haiku",
    "claude-3-opus-20240229":                        "claude-3-opus",
    # Bedrock EU regional variants → same intelligence as the base model.
    "eu.anthropic.claude-opus-4-7-v1:0":             "claude-opus-4-7",
    "eu.anthropic.claude-sonnet-4-6-v1:0":           "claude-sonnet-4-6",
    "eu.anthropic.claude-haiku-4-5-v1:0":            "claude-4-5-haiku",
    "eu.anthropic.claude-3-7-sonnet-20250219-v1:0":  "claude-3-7-sonnet",
    "eu.anthropic.claude-3-5-haiku-20241022-v1:0":   "claude-3-5-haiku",

    # ── Google ───────────────────────────────────────────────────────────────
    "gemini-3.1-pro-preview":                        "gemini-3-1-pro-preview",
    "gemini-3-flash-preview":                        "gemini-3-flash",
    "gemini-3.1-flash-lite-preview":                 "gemini-3-1-flash-lite-preview",
    "gemini-2.5-pro":                                "gemini-2-5-pro",
    "gemini-2.5-flash":                              "gemini-2-5-flash",
    "gemini-2.5-flash-lite":                         "gemini-2-5-flash-lite",
    "gemini-2.0-flash":                              "gemini-2-0-flash",
    "gemini-2.0-flash-lite":                         "gemini-2-0-flash-lite-001",
    "gemini-1.5-pro":                                "gemini-1-5-pro",
    "gemini-1.5-flash":                              "gemini-1-5-flash",
    "gemini-1.5-flash-8b":                           "gemini-1-5-flash-8b",

    # ── Mistral ──────────────────────────────────────────────────────────────
    "mistral-large-latest":                          "mistral-large-2",
    "mistral-large-3":                               "mistral-large-3",
    "mistral-medium-latest":                         "mistral-medium-3",
    "mistral-small-latest":                          "mistral-small-3",
    "magistral-medium-latest":                       "magistral-medium",
    "magistral-small-latest":                        "magistral-small",
    "devstral-medium-latest":                        "devstral-medium",
    "ministral-8b-latest":                           "ministral-3-8b",
    "ministral-3b-latest":                           "ministral-3-3b",
    "open-mixtral-8x22b":                            "mistral-8x22b-instruct",
    "pixtral-large-latest":                          "pixtral-large-2411",
    # codestral-latest: dropped from AA v4 catalogue; seeded value stays.

    # ── DeepSeek ─────────────────────────────────────────────────────────────
    "deepseek-chat":                                 "deepseek-v3",
    "deepseek-v3.1":                                 "deepseek-v3-1",
    "deepseek-v3.2-exp":                             "deepseek-v3-2",
    "deepseek-reasoner":                             "deepseek-r1",
    # deepseek-r2: not in AA.

    # ── Meta (Llama family across providers) ─────────────────────────────────
    "meta-llama/Llama-4-Maverick-17B-128E-Instruct":          "llama-4-maverick",
    "meta-llama/Llama-4-Scout-17B-16E-Instruct":              "llama-4-scout",
    "meta-llama/llama-4-maverick-17b-128e-instruct":          "llama-4-maverick",
    "meta-llama/llama-4-scout-17b-16e-instruct":              "llama-4-scout",
    "meta-llama/Llama-4-Maverick-17B-128E-Instruct-Turbo":    "llama-4-maverick",
    "llama-3.3-70b-versatile":                                "llama-3-3-instruct-70b",
    "llama-3.1-8b-instant":                                   "llama-3-1-instruct-8b",
    "deepseek-r1-distill-llama-70b":                          "deepseek-r1-distill-llama-70b",
    "llama3.1-8b":                                            "llama-3-1-instruct-8b",
    "llama-3.3-70b":                                          "llama-3-3-instruct-70b",
    "meta-llama/Meta-Llama-3.1-405B-Instruct-Turbo":          "llama-3-1-instruct-405b",
    "meta-llama/Llama-3.3-70B-Instruct-Turbo":                "llama-3-3-instruct-70b",
    "meta-llama/llama-3.3-70b-instruct":                      "llama-3-3-instruct-70b",
    "eu.meta.llama3-3-70b-instruct-v1:0":                     "llama-3-3-instruct-70b",
    # gemma2-9b-it, llama-4-behemoth: not in AA v4.

    # ── Qwen ─────────────────────────────────────────────────────────────────
    "qwen-2.5-72b-instruct":                                  "qwen2-5-72b-instruct",
    "Qwen/Qwen2.5-72B-Instruct-Turbo":                        "qwen2-5-72b-instruct",
    "qwen/qwen3-32b":                                         "qwen3-32b-instruct",
    "qwen-max":                                               "qwen3-max",
    "qwen-turbo":                                             "qwen-turbo",
    "qwen3-max":                                              "qwen3-max",
    "qwen3-235b-a22b":                                        "qwen3-235b-a22b-instruct",
    "qwen3-coder-plus":                                       "qwen3-coder-480b-a35b-instruct",
    "qwen3-vl-plus":                                          "qwen3-vl-235b-a22b-instruct",
    "Qwen/Qwen3-Coder-480B-A35B-Instruct":                    "qwen3-coder-480b-a35b-instruct",
    "Qwen/Qwen3-235B-A22B-Thinking-2507":                     "qwen3-235b-a22b-instruct-reasoning",
    "Qwen/Qwen3-235B-A22B-Instruct-2507":                     "qwen3-235b-a22b-instruct-2507",
    # qwen-plus: no clean match in AA v4.

    # ── xAI ──────────────────────────────────────────────────────────────────
    "grok-4.20-0309-reasoning":                               "grok-4-20",
    "grok-4.20-0309-non-reasoning":                           "grok-4-20-non-reasoning",
    "grok-4-1-fast-reasoning":                                "grok-4-1-fast-reasoning",
    "grok-4-1-fast-non-reasoning":                            "grok-4-1-fast",
    "grok-3":                                                 "grok-3",
    "x-ai/grok-4":                                            "grok-4",
    # grok-3-mini, grok-2-vision-1212, grok-4.20-multi-agent-0309: not in AA.

    # ── Cohere ───────────────────────────────────────────────────────────────
    "command-a-03-2025":                                      "command-a",
    "command-r-plus-08-2024":                                 "command-r-plus-04-2024",
    "command-r-08-2024":                                      "command-r-03-2024",

    # ── Perplexity ───────────────────────────────────────────────────────────
    "sonar-pro":                                              "sonar-pro",
    "sonar":                                                  "sonar",

    # ── Moonshot ─────────────────────────────────────────────────────────────
    "kimi-k2-0905":                                           "kimi-k2",
    "kimi-k2-thinking":                                       "kimi-k2-thinking",
    "moonshotai/kimi-k2-instruct":                            "kimi-k2",
    "moonshotai/kimi-k2":                                     "kimi-k2",
    "moonshotai/kimi-k2-instruct-0905":                       "kimi-k2",
    "moonshotai/Kimi-K2-Instruct":                            "kimi-k2",
    "moonshotai/Kimi-K2-Thinking":                            "kimi-k2-thinking",
    "Pro/moonshotai/Kimi-K2.5":                               "kimi-k2-5",
    "accounts/fireworks/models/kimi-k2-5":                    "kimi-k2-5",
    "accounts/fireworks/models/kimi-k2-5-turbo":              "kimi-k2-5",

    # ── Z.ai (GLM) ───────────────────────────────────────────────────────────
    "glm-4.6":                                                "glm-4-6",
    "glm-4.5-air":                                            "glm-4-5-air",
    "glm-5":                                                  "glm-5",
    "glm-5.1":                                                "glm-5-1",
    "zai-org/GLM-4.6":                                        "glm-4-6",
    "Pro/zai-org/GLM-5":                                      "glm-5",
    "accounts/fireworks/models/glm-5":                        "glm-5",
    "accounts/fireworks/models/glm-5-1":                      "glm-5-1",
    "accounts/fireworks/models/glm-4p5-air":                  "glm-4-5-air",

    # ── NVIDIA / Nemotron ────────────────────────────────────────────────────
    "nvidia/llama-3.3-nemotron-super-49b-v1":                 "llama-3-3-nemotron-super-49b",
    "nvidia/nemotron-nano-9b-v2":                             "nvidia-nemotron-nano-9b-v2",

    # ── ERNIE / Baidu ────────────────────────────────────────────────────────
    "baidu/ERNIE-4.5-300B-A47B":                              "ernie-4-5-300b-a47b",

    # ── Amazon Nova (Bedrock EU) ─────────────────────────────────────────────
    "eu.amazon.nova-pro-v1:0":                                "nova-pro",
    "eu.amazon.nova-lite-v1:0":                               "nova-lite",
    "eu.amazon.nova-micro-v1:0":                              "nova-micro",

    # ── Mistral on Bedrock EU ────────────────────────────────────────────────
    "eu.mistral.mistral-large-2402-v1:0":                     "mistral-large-2407",

    # ── OpenRouter / Replicate passthroughs ──────────────────────────────────
    "anthropic/claude-opus-4.6":                              "claude-opus-4-6",
    "anthropic/claude-sonnet-4.5":                            "claude-4-5-sonnet",
    "anthropic/claude-haiku-4.5":                             "claude-4-5-haiku",
    "anthropic/claude-3.7-sonnet":                            "claude-3-7-sonnet",
    "openai/gpt-5":                                           "gpt-5",
    "openai/gpt-5-nano":                                      "gpt-5-nano",
    "openai/gpt-4.1":                                         "gpt-4-1",
    "openai/gpt-4.1-mini":                                    "gpt-4-1-mini",
    "google/gemini-3-flash-preview":                          "gemini-3-flash",
    "google/gemini-2.5-pro":                                  "gemini-2-5-pro",
    "google/gemini-2.5-flash":                                "gemini-2-5-flash",
    "google/gemma-4-31b-it":                                  "gemma-4-31b",
    "mistralai/mistral-large":                                "mistral-large-2",
    "deepseek/deepseek-chat":                                 "deepseek-v3",
    "deepseek/deepseek-r1":                                   "deepseek-r1",
    "deepseek-ai/DeepSeek-V3":                                "deepseek-v3",
    "deepseek-ai/DeepSeek-V3.1":                              "deepseek-v3-1",
    "deepseek-ai/DeepSeek-V3.2":                              "deepseek-v3-2",
    "deepseek-ai/DeepSeek-R1":                                "deepseek-r1",
    "deepseek-ai/deepseek-r1":                                "deepseek-r1",
    "MiniMaxAI/MiniMax-M2.5":                                 "minimax-m2-5",
    "accounts/fireworks/models/minimax-m2-5":                 "minimax-m2-5",

    # ── Groq aliases ─────────────────────────────────────────────────────────
    "openai/gpt-oss-120b":                                    "gpt-oss-120b",
    "openai/gpt-oss-20b":                                     "gpt-oss-20b",

    # ── Fireworks passthroughs ───────────────────────────────────────────────
    "accounts/fireworks/models/gpt-oss-120b":                 "gpt-oss-120b",
    "accounts/fireworks/models/gpt-oss-20b":                  "gpt-oss-20b",
    "accounts/fireworks/models/llama4-maverick-instruct-basic":    "llama-4-maverick",
    "accounts/fireworks/models/deepseek-v3p2-exp":            "deepseek-v3-2",
    "accounts/fireworks/models/qwen3-coder-480b-a35b-instruct":    "qwen3-coder-480b-a35b-instruct",
    "accounts/fireworks/models/llama-v3p1-405b-instruct":     "llama-3-1-instruct-405b",

    # Models with no AA v4 entry (embeddings, Hunyuan, Pharia, …) keep
    # their seeded quality_index; deliberately absent from this map.
}


# Override map: catalogue `id` → Arena leaderboard slug, used only when
# the normalize-and-match fallback in _match_arena_elo fails to find the
# right row (e.g. when Arena uses a model nickname our catalogue doesn't).
# Values are run through _arena_normalize before lookup, so both formats
# (dotted "gpt-4.1" or dashed "gpt-4-1") are accepted here. The new
# wulong.dev feed uses slimmer slugs than the retired LMSYS endpoint, so
# this map is intentionally near-empty — populate as you spot mismatches.
ARENA_NAMES: dict = {}

_aa_cache: dict = {}
_aa_tps_cache: dict = {}
_arena_cache: dict = {}


def _extract_aa_score(item: dict):
    """Pull the AA Intelligence Index out of either the v4 shape or older
    flat shape, so the scraper is resilient to schema drift between
    Artificial Analysis catalogue versions."""
    evals = item.get("evaluations") or {}
    candidates = (
        evals.get("artificial_analysis_intelligence_index"),
        evals.get("intelligence_index"),
        item.get("intelligence_index"),
        item.get("quality_index"),
    )
    for v in candidates:
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def _extract_aa_tps(item: dict):
    """AA's median output tokens/sec. Same defensive multi-key lookup."""
    candidates = (
        item.get("median_output_tokens_per_second"),
        item.get("output_tokens_per_second_median"),
        item.get("output_tokens_per_second"),
        (item.get("performance") or {}).get("median_output_tokens_per_second"),
    )
    for v in candidates:
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f > 0:
            return f
    return None


async def fetch_aa_index() -> tuple[dict, dict]:
    """
    Fetch Artificial Analysis Intelligence Index + measured throughput.

    AA's free-tier API requires the AA_API_KEY env var (sign up at
    https://artificialanalysis.ai). If the key is unset we skip the
    fetch with a warning — seeded values keep ranks sensible until the
    key is provided.

    Returns (score_by_slug, tps_by_slug). Either may be empty if AA
    didn't expose that field for a model.
    """
    global _aa_cache, _aa_tps_cache
    api_key = os.environ.get("AA_API_KEY")
    if not api_key:
        logger.warning("AA Index: AA_API_KEY not set; skipping live fetch")
        return _aa_cache, _aa_tps_cache
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                AA_API_URL,
                headers={"Accept": "application/json", "x-api-key": api_key},
            )
            if resp.status_code == 200:
                data = resp.json()
                rows = data.get("data") if isinstance(data, dict) else data
                if not isinstance(rows, list):
                    rows = []
                scores: dict = {}
                tps: dict = {}
                for item in rows:
                    # Prefer the human-readable `slug` (e.g. "gpt-5-4") as
                    # the dict key — AA's `id` is a UUID and AA_SLUGS in
                    # this module is written against slug values.
                    slug = item.get("slug") or item.get("id") or ""
                    if not slug:
                        continue
                    score = _extract_aa_score(item)
                    if score is not None:
                        scores[slug] = score
                    measured_tps = _extract_aa_tps(item)
                    if measured_tps is not None:
                        tps[slug] = measured_tps
                if scores:
                    _aa_cache = scores
                    _aa_tps_cache = tps
                    logger.info("AA Index: fetched %d scores, %d throughput", len(scores), len(tps))
                    return scores, tps
                logger.warning("AA Index: empty payload from %s", AA_API_URL)
            else:
                logger.warning("AA Index: HTTP %d from %s", resp.status_code, AA_API_URL)
    except Exception as e:
        logger.warning("AA Index fetch failed: %s", e)
    return _aa_cache, _aa_tps_cache


async def fetch_arena_elo() -> dict:
    """
    Fetch LMArena (formerly LMSYS Chatbot Arena) ELO scores.

    LMArena's official API was removed when they rebranded in early 2026;
    we now read from a community-maintained daily snapshot of the
    official leaderboard. See module docstring for the source.

    Returns dict: {normalized_model_name: elo_score}
    """
    global _arena_cache
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(
                ARENA_API_URL,
                headers={"Accept": "application/json"},
            )
            if resp.status_code == 200:
                data = resp.json()
                rows = (
                    data.get("models")
                    or data.get("leaderboard")
                    or data.get("data")
                    or (data if isinstance(data, list) else [])
                )
                result: dict = {}
                for item in rows:
                    name = item.get("model") or item.get("model_name") or ""
                    elo = item.get("score") or item.get("rating") or item.get("elo") or item.get("arena_score")
                    if not name or elo is None:
                        continue
                    try:
                        result[_arena_normalize(name)] = float(elo)
                    except (TypeError, ValueError):
                        continue
                if result:
                    _arena_cache = result
                    logger.info("Arena ELO: fetched %d scores", len(result))
                    return result
                logger.warning("Arena ELO: empty payload from %s", ARENA_API_URL)
            else:
                logger.warning("Arena ELO: HTTP %d from %s", resp.status_code, ARENA_API_URL)
    except Exception as e:
        logger.warning("Arena ELO fetch failed: %s", e)
    return _arena_cache


def _arena_normalize(name: str) -> str:
    """Squash leaderboard names down to a single canonical form so
    catalogue lookups don't fail on dot/dash/space variants. Example:
        "Claude Opus 4.6 Thinking" → "claude-opus-4-6-thinking"
        "claude-opus-4-6-thinking" → "claude-opus-4-6-thinking"
    """
    s = name.strip().lower()
    for ch in (".", "_", " ", "/"):
        s = s.replace(ch, "-")
    while "--" in s:
        s = s.replace("--", "-")
    return s.strip("-")


def _match_arena_elo(model: dict, arena_data: dict):
    """Resolve a model to its Arena ELO via three increasingly-loose paths,
    in order:

    1. Explicit map (ARENA_NAMES) — wins outright when present.
    2. Normalized exact match on the catalogue's display `model` name.
    3. Normalized exact match on the catalogue's `model_id`.

    Substring matching is intentionally avoided — "gpt-4" would otherwise
    collide with multiple gpt-4* variants and the first dict-iteration
    hit would win at random.
    """
    explicit = ARENA_NAMES.get(model["id"])
    if explicit:
        score = arena_data.get(_arena_normalize(explicit))
        if score is not None:
            return score
    score = arena_data.get(_arena_normalize(model.get("model", "")))
    if score is not None:
        return score
    return arena_data.get(_arena_normalize(model.get("model_id", "")))


def apply_live_scores(models: list, aa_data: dict, aa_tps: dict, arena_data: dict) -> tuple[list, dict]:
    """
    Overwrite aa_index, aa_tps and arena_elo on each model with live-fetched
    values where a match is found. Seeded values remain as fallback. Returns
    the model list along with a count of how many entries each source
    actually updated — useful for spotting silent upstream failures.
    """
    counts = {"aa": 0, "aa_tps": 0, "arena": 0}
    for m in models:
        slug = AA_SLUGS.get(m["model_id"])
        if slug:
            if slug in aa_data:
                m["aa_index"] = round(aa_data[slug], 1)
                counts["aa"] += 1
            if slug in aa_tps:
                m["aa_tps"] = int(round(aa_tps[slug]))
                counts["aa_tps"] += 1

        elo = _match_arena_elo(m, arena_data)
        if elo is not None:
            m["arena_elo"] = int(elo)
            counts["arena"] += 1
    return models, counts


async def refresh_quality_indices(models: list) -> tuple[list, dict]:
    aa_data, aa_tps = await fetch_aa_index()
    arena_data = await fetch_arena_elo()
    counts = {"aa": 0, "aa_tps": 0, "arena": 0}
    if aa_data or aa_tps or arena_data:
        models, counts = apply_live_scores(models, aa_data, aa_tps, arena_data)
    return models, counts
