"""
Fetches quality index data from public leaderboards:
  - Artificial Analysis Intelligence Index (artificialanalysis.ai)
  - LMSYS Chatbot Arena ELO (lmarena.ai)

Falls back to seeded values in providers.py if fetching fails.
"""

import logging
import httpx

logger = logging.getLogger(__name__)

# ── Model ID mappings ─────────────────────────────────────────────────────────
# Maps our internal model_id → slug used by each leaderboard

AA_SLUGS = {
    "gpt-5.4":                          "gpt-5-4",
    "gpt-5.4-mini":                     "gpt-5-4-mini",
    "gpt-5.4-nano":                     "gpt-5-4-nano",
    "gpt-5.4-pro":                      "gpt-5-4-pro",
    "gpt-5.3-chat-latest":              "gpt-5-3-chat",
    "gpt-5.3-codex":                    "gpt-5-3-codex",
    "o3-deep-research":                 "o3-deep-research",
    "o4-mini-deep-research":            "o4-mini-deep-research",
    "gpt-5":                            "gpt-5",
    "gpt-5-mini":                       "gpt-5-mini",
    "gpt-5-nano":                       "gpt-5-nano",
    "gpt-5-pro":                        "gpt-5-pro",
    "gpt-4.1":                          "gpt-4-1",
    "gpt-4.1-mini":                     "gpt-4-1-mini",
    "gpt-4.1-nano":                     "gpt-4-1-nano",
    "gpt-4o":                           "gpt-4o",
    "gpt-4o-mini":                      "gpt-4o-mini",
    "o1":                               "o1",
    "o1-mini":                          "o1-mini",
    "o3":                               "o3",
    "o3-mini":                          "o3-mini",
    "o3-pro":                           "o3-pro",
    "o4-mini":                          "o4-mini",
    "claude-opus-4-7":                  "claude-opus-4-7",
    "claude-opus-4-5":                  "claude-opus-4-5",
    "claude-sonnet-4-6":                "claude-sonnet-4-6",
    "claude-sonnet-4-5":                "claude-sonnet-4-5",
    "claude-3-7-sonnet-20250219":       "claude-3-7-sonnet",
    "claude-3-5-sonnet-20241022":       "claude-3-5-sonnet",
    "claude-haiku-4-5":                 "claude-haiku-4",
    "claude-3-5-haiku-20241022":        "claude-3-5-haiku",
    "claude-3-opus-20240229":           "claude-3-opus",
    "gemini-3.1-pro-preview":           "gemini-3-1-pro",
    "gemini-3-flash-preview":           "gemini-3-flash",
    "gemini-3.1-flash-lite-preview":    "gemini-3-1-flash-lite",
    "gemini-2.5-pro":                   "gemini-2-5-pro",
    "gemini-2.5-flash":                 "gemini-2-5-flash",
    "gemini-2.5-flash-lite":            "gemini-2-5-flash-lite",
    "gemini-2.0-flash":                 "gemini-2-flash",
    "gemini-2.0-flash-lite":            "gemini-2-flash-lite",
    "gemini-1.5-pro":                   "gemini-1-5-pro",
    "gemini-1.5-flash":                 "gemini-1-5-flash",
    "gemini-1.5-flash-8b":              "gemini-1-5-flash-8b",
    "mistral-large-latest":             "mistral-large-2",
    "mistral-large-3":                  "mistral-large-3",
    "mistral-medium-latest":            "mistral-medium-3",
    "mistral-small-latest":             "mistral-small-3",
    "magistral-medium-latest":          "magistral-medium",
    "magistral-small-latest":           "magistral-small",
    "devstral-medium-latest":           "devstral",
    "ministral-8b-latest":              "ministral-8b",
    "ministral-3b-latest":              "ministral-3b",
    "codestral-latest":                 "codestral-2501",
    "open-mixtral-8x22b":               "mixtral-8x22b",
    "deepseek-chat":                    "deepseek-v3",
    "deepseek-v3.1":                    "deepseek-v3-1",
    "deepseek-v3.2-exp":                "deepseek-v3-2-exp",
    "deepseek-reasoner":                "deepseek-r1",
    "deepseek-r2":                      "deepseek-r2",
    "meta-llama/Llama-4-Maverick-17B-128E-Instruct": "llama-4-maverick",
    "meta-llama/Llama-4-Scout-17B-16E-Instruct":     "llama-4-scout",
    "llama-3.3-70b-versatile":          "llama-3-3-70b",
    "llama-3.1-8b-instant":             "llama-3-1-8b",
    "deepseek-r1-distill-llama-70b":    "deepseek-r1-distill-70b",
    "qwen-2.5-72b-instruct":            "qwen-2-5-72b",
    "gemma2-9b-it":                     "gemma-2-9b",
    "llama3.1-8b":                      "llama-3-1-8b",
    "llama-3.3-70b":                    "llama-3-3-70b",
    "meta-llama/Meta-Llama-3.1-405B-Instruct-Turbo": "llama-3-1-405b",
    "meta-llama/Llama-3.3-70B-Instruct-Turbo":       "llama-3-3-70b",
    "deepseek-ai/DeepSeek-V3":          "deepseek-v3",
    "Qwen/Qwen2.5-72B-Instruct-Turbo":  "qwen-2-5-72b",
    "grok-4.20-0309-reasoning":         "grok-4-20-reasoning",
    "grok-4.20-0309-non-reasoning":     "grok-4-20-non-reasoning",
    "grok-4.20-multi-agent-0309":       "grok-4-20-multi-agent",
    "grok-4-1-fast-reasoning":          "grok-4-1-fast-reasoning",
    "grok-4-1-fast-non-reasoning":      "grok-4-1-fast-non-reasoning",
    "grok-3":                           "grok-3",
    "grok-3-mini":                      "grok-3-mini",
    "grok-2-vision-1212":               "grok-2-vision",
    "command-a-03-2025":                "command-a",
    "command-r-plus-08-2024":           "command-r-plus",
    "command-r-08-2024":                "command-r",
    "sonar-pro":                        "sonar-pro",
    "sonar":                            "sonar",
    "qwen-max":                         "qwen-max",
    "qwen-plus":                        "qwen-plus",
    "qwen3-max":                        "qwen3-max",
    "qwen3-235b-a22b":                  "qwen3-235b-a22b",
    "qwen3-coder-plus":                 "qwen3-coder",
    "qwen3-vl-plus":                    "qwen3-vl",
    "kimi-k2-0905":                     "kimi-k2",
    "kimi-k2-thinking":                 "kimi-k2-thinking",
    "moonshotai/kimi-k2-instruct":      "kimi-k2",
    "glm-4.6":                          "glm-4-6",
    "glm-4.5-air":                      "glm-4-5-air",
    "nvidia/llama-3.3-nemotron-super-49b-v1": "nemotron-llama-3-3",
    "nvidia/nemotron-nano-9b-v2":       "nemotron-nano-9b",
    "meta-llama/Llama-4-Behemoth":      "llama-4-behemoth",
    # OpenRouter passthrough slugs — map to the underlying AA model slug.
    "anthropic/claude-opus-4.6":        "claude-opus-4-6",
    "anthropic/claude-sonnet-4.5":      "claude-sonnet-4-5",
    "anthropic/claude-haiku-4.5":       "claude-haiku-4",
    "openai/gpt-5":                     "gpt-5",
    "openai/gpt-5-nano":                "gpt-5-nano",
    "openai/gpt-4.1":                   "gpt-4-1",
    "openai/gpt-4.1-mini":              "gpt-4-1-mini",
    "google/gemini-3-flash-preview":    "gemini-3-flash",
    "google/gemini-2.5-pro":            "gemini-2-5-pro",
    "google/gemini-2.5-flash":          "gemini-2-5-flash",
    "mistralai/mistral-large":          "mistral-large-2",
    "x-ai/grok-4":                      "grok-4-20-reasoning",
    "moonshotai/kimi-k2":               "kimi-k2",
    "deepseek/deepseek-chat":           "deepseek-v3",
    "deepseek/deepseek-r1":             "deepseek-r1",
    "meta-llama/llama-3.3-70b-instruct": "llama-3-3-70b",
    # Groq new entries
    "openai/gpt-oss-120b":              "gpt-oss-120b",
    "openai/gpt-oss-20b":               "gpt-oss-20b",
    "qwen/qwen3-32b":                   "qwen-3-32b",
    "moonshotai/kimi-k2-instruct-0905": "kimi-k2",
    # Fireworks/Together passthroughs
    "accounts/fireworks/models/kimi-k2-5":        "kimi-k2-5",
    "accounts/fireworks/models/kimi-k2-5-turbo":  "kimi-k2-5-turbo",
    "accounts/fireworks/models/glm-5":            "glm-5",
    "accounts/fireworks/models/glm-5-1":          "glm-5-1",
    "accounts/fireworks/models/gpt-oss-120b":     "gpt-oss-120b",
    "accounts/fireworks/models/gpt-oss-20b":      "gpt-oss-20b",
    "accounts/fireworks/models/minimax-m2-5":     "minimax-m2-5",
    "deepseek-ai/DeepSeek-V3.1":        "deepseek-v3-1",
    "deepseek-ai/DeepSeek-R1":          "deepseek-r1",
    "Qwen/Qwen3-Coder-480B-A35B-Instruct": "qwen-3-coder-480b",
    "Qwen/Qwen3-235B-A22B-Thinking-2507": "qwen-3-235b-thinking",
    "Qwen/Qwen3-235B-A22B-Instruct-2507": "qwen-3-235b-instruct",
    "moonshotai/Kimi-K2-Instruct":      "kimi-k2",
    "moonshotai/Kimi-K2-Thinking":      "kimi-k2-thinking",
    "google/gemma-4-31b-it":            "gemma-4-31b",
    "MiniMaxAI/MiniMax-M2.5":           "minimax-m2-5",
    # Replicate
    "anthropic/claude-3.7-sonnet":      "claude-3-7-sonnet",
    "deepseek-ai/deepseek-r1":          "deepseek-r1",
    # SiliconFlow
    "deepseek-ai/DeepSeek-V3.2":        "deepseek-v3-2",
    "Pro/moonshotai/Kimi-K2.5":         "kimi-k2-5",
    "zai-org/GLM-4.6":                  "glm-4-6",
    "Pro/zai-org/GLM-5":                "glm-5",
    "tencent/Hunyuan-A13B-Instruct":    "hunyuan-a13b",
    "baidu/ERNIE-4.5-300B-A47B":        "ernie-4-5-300b",
    # Bedrock EU variants already covered via AA slug match on base model
}


# Maps our catalogue `id` (slug) → exact lmarena.ai leaderboard model_name
# (lowercased). Substring matching was previously used here, but it
# silently mis-attributed scores across model families ("gpt-4" matching
# both "gpt-4o" and "gpt-4.1"). Anything not in this map simply won't
# receive a live Arena ELO — the seeded value in providers.py remains.
ARENA_NAMES: dict = {
    "openai-gpt-4-1":          "gpt-4.1-2025-04-14",
    "openai-gpt-4-1-mini":     "gpt-4.1-mini-2025-04-14",
    "openai-gpt-4-1-nano":     "gpt-4.1-nano-2025-04-14",
    "openai-gpt-4o":           "gpt-4o-2024-08-06",
    "openai-gpt-4o-mini":      "gpt-4o-mini-2024-07-18",
    "openai-o1":               "o1-2024-12-17",
    "openai-o1-mini":          "o1-mini",
    "openai-o3":               "o3-2025-04-16",
    "openai-o3-mini":          "o3-mini",
    "openai-o4-mini":          "o4-mini-2025-04-16",
}

_aa_cache: dict = {}
_aa_tps_cache: dict = {}
_arena_cache: dict = {}


# AA payload field names we'll accept for throughput. They have shifted
# across catalogue versions; we try the most specific first.
_AA_TPS_FIELDS = (
    "output_tokens_per_second_median",
    "median_output_tokens_per_second",
    "output_tokens_per_second",
    "median_throughput",
    "throughput",
)


def _extract_aa_tps(item: dict):
    for key in _AA_TPS_FIELDS:
        val = item.get(key)
        if val is not None:
            try:
                v = float(val)
            except (TypeError, ValueError):
                continue
            if v > 0:
                return v
    return None


async def fetch_aa_index() -> tuple[dict, dict]:
    """
    Fetch Artificial Analysis Intelligence Index scores + measured throughput.

    Returns (score_by_slug, tps_by_slug). Either may be empty if AA didn't
    expose that field for a given model, but the score is preserved
    independently from throughput so a partial payload is still usable.
    """
    global _aa_cache, _aa_tps_cache
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://artificialanalysis.ai/api/models",
                headers={"Accept": "application/json"},
            )
            if resp.status_code == 200:
                data = resp.json()
                scores: dict = {}
                tps: dict = {}
                for item in data if isinstance(data, list) else data.get("models", []):
                    slug = item.get("slug") or item.get("id") or ""
                    if not slug:
                        continue
                    score = item.get("intelligence_index") or item.get("quality_index")
                    if score is not None:
                        scores[slug] = float(score)
                    measured_tps = _extract_aa_tps(item)
                    if measured_tps is not None:
                        tps[slug] = measured_tps
                if scores:
                    _aa_cache = scores
                    _aa_tps_cache = tps
                    logger.info("AA Index: fetched %d scores, %d throughput", len(scores), len(tps))
                    return scores, tps
    except Exception as e:
        logger.warning("AA Index fetch failed: %s", e)
    return _aa_cache, _aa_tps_cache


async def fetch_arena_elo() -> dict:
    """
    Fetch LMSYS Chatbot Arena ELO scores.
    Returns dict: {model_name: elo_score}
    """
    global _arena_cache
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://lmarena.ai/api/leaderboard",
                headers={"Accept": "application/json"},
            )
            if resp.status_code == 200:
                data = resp.json()
                result = {}
                rows = data if isinstance(data, list) else data.get("leaderboard", data.get("data", []))
                for item in rows:
                    name = item.get("model_name") or item.get("model") or ""
                    elo = item.get("rating") or item.get("elo") or item.get("arena_score")
                    if name and elo:
                        result[name.lower()] = float(elo)
                if result:
                    _arena_cache = result
                    logger.info("Arena ELO: fetched %d scores", len(result))
                    return result
    except Exception as e:
        logger.warning("Arena ELO fetch failed: %s", e)
    return _arena_cache


def _match_arena_elo(model: dict, arena_data: dict):
    """Resolve a model to its Arena ELO via explicit map, then exact name match.

    Substring matching is intentionally avoided — "gpt-4" would otherwise
    collide with multiple gpt-4* variants and the first dict-iteration hit
    would win at random.
    """
    explicit = ARENA_NAMES.get(model["id"])
    if explicit:
        score = arena_data.get(explicit.lower())
        if score is not None:
            return score
    return arena_data.get(model["model"].lower())


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
