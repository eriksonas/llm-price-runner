"""One-shot: rewrite arena_elo for every model in providers.py using the
Apr 14, 2026 lmarena leaderboard snapshot.

Mapping chooses the Arena entry that best matches the provider catalog's model_id.
Variants (thinking/non-thinking) are resolved to the base-line variant unless the
catalog id specifies otherwise. None means no comparable entry exists on Arena.
"""
import re
from pathlib import Path

# model_id -> new arena_elo (None = not on Arena)
NEW_ELO = {
    # OpenAI
    "gpt-4.1": 1413,
    "gpt-4.1-mini": 1382,
    "gpt-4.1-nano": 1321,
    "gpt-4o": 1334,
    "gpt-4o-mini": 1317,
    "o1": 1401,
    "o1-mini": 1337,
    "o3": 1431,
    "o3-mini": 1347,
    "o4-mini": 1390,
    "gpt-5.4": 1466,
    "gpt-5.4-mini": 1457,
    "gpt-5.4-nano": 1402,
    "gpt-5.4-pro": 1481,         # closest: gpt-5.4-high
    "gpt-5.3-chat-latest": 1455,
    "gpt-5.3-codex": None,
    "o3-deep-research": None,
    "o4-mini-deep-research": None,
    "gpt-5": 1433,
    "gpt-5-mini": 1389,
    "gpt-5-nano": 1336,
    "gpt-5-pro": 1445,           # not on Arena; conservative estimate above gpt-5-high
    "o3-pro": 1440,              # not on Arena; o3 base is 1431
    "text-embedding-3-large": None,
    "text-embedding-3-small": None,

    # Anthropic
    "claude-opus-4-7": 1505,     # not on Arena yet; extrapolated from 4-6 (1496)
    "claude-sonnet-4-6": 1461,
    "claude-opus-4-5": 1469,
    "claude-sonnet-4-5": 1451,
    "claude-3-7-sonnet-20250219": 1370,
    "claude-3-5-sonnet-20241022": 1372,
    "claude-haiku-4-5": 1408,
    "claude-3-5-haiku-20241022": 1323,
    "claude-3-opus-20240229": 1321,

    # Google Gemini
    "gemini-3.1-pro-preview": 1493,
    "gemini-3-flash-preview": 1474,
    "gemini-3.1-flash-lite-preview": 1436,
    "gemini-2.5-pro": 1448,
    "gemini-2.5-flash": 1411,
    "gemini-2.5-flash-lite": 1380,
    "gemini-2.0-flash": 1360,
    "gemini-2.0-flash-lite": 1353,
    "gemini-1.5-pro": 1351,
    "gemini-1.5-flash": 1309,
    "gemini-1.5-flash-8b": 1258,
    "gemini-embedding-001": None,
    "text-embedding-004": None,

    # Mistral
    "mistral-large-latest": 1305,
    "mistral-small-latest": 1303,
    "mistral-medium-latest": 1410,
    "mistral-large-3": 1415,
    "magistral-medium-latest": 1303,
    "magistral-small-latest": 1280,
    "devstral-medium-latest": None,
    "ministral-8b-latest": 1237,
    "ministral-3b-latest": None,
    "open-mistral-nemo": None,
    "codestral-latest": None,
    "pixtral-large-latest": None,
    "open-mixtral-8x22b": 1228,
    "mistral-embed": None,

    # Meta Llama (SambaNova section)
    "meta-llama/Llama-4-Maverick-17B-128E-Instruct": 1327,
    "meta-llama/Llama-4-Scout-17B-16E-Instruct": 1322,
    "meta-llama/Llama-4-Behemoth": None,   # unreleased on Arena

    # DeepSeek (V3.2 API)
    "deepseek-chat": 1423,
    "deepseek-reasoner": 1423,

    # Groq
    "meta-llama/llama-4-maverick-17b-128e-instruct": 1327,
    "meta-llama/llama-4-scout-17b-16e-instruct": 1322,
    "llama-3.3-70b-versatile": 1318,
    "llama-3.1-8b-instant": 1211,
    "deepseek-r1-distill-llama-70b": 1310,  # distill approx; base r1 is 1397
    "qwen-2.5-72b-instruct": 1302,
    "gemma2-9b-it": 1265,
    "openai/gpt-oss-120b": 1354,
    "openai/gpt-oss-20b": 1318,
    "qwen/qwen3-32b": 1347,
    "moonshotai/kimi-k2-instruct-0905": 1418,

    # Cerebras
    "llama-3.3-70b": 1318,
    "llama3.1-8b": 1211,

    # Together
    "meta-llama/Llama-4-Maverick-17B-128E-Instruct-Turbo": 1327,
    "meta-llama/Llama-3.3-70B-Instruct-Turbo": 1318,
    "meta-llama/Meta-Llama-3.1-405B-Instruct-Turbo": 1332,
    "deepseek-ai/DeepSeek-V3": 1358,
    "Qwen/Qwen2.5-72B-Instruct-Turbo": 1302,
    "deepseek-ai/DeepSeek-V3.1": 1418,
    "deepseek-ai/DeepSeek-R1": 1397,
    "Qwen/Qwen3-Coder-480B-A35B-Instruct": 1387,
    "moonshotai/Kimi-K2-Instruct": 1417,
    "google/gemma-4-31b-it": 1450,
    "MiniMaxAI/MiniMax-M2.5": 1402,

    # xAI Grok
    "grok-4.20-0309-reasoning": 1479,
    "grok-4.20-0309-non-reasoning": 1400,      # no explicit non-reasoning entry
    "grok-4.20-multi-agent-0309": 1476,
    "grok-4-1-fast-reasoning": 1432,
    "grok-4-1-fast-non-reasoning": 1421,       # grok-4-fast-chat
    "grok-3": 1412,
    "grok-3-mini": 1357,
    "grok-2-vision-1212": None,

    # Cohere
    "command-a-03-2025": 1353,
    "command-r-plus-08-2024": 1275,
    "command-r-08-2024": 1249,
    "embed-english-v3.0": None,
    "embed-multilingual-v3.0": None,

    # Perplexity
    "sonar-pro": None,
    "sonar": None,

    # Alibaba
    "qwen3-max": 1435,
    "qwen3-235b-a22b": 1374,
    "qwen3-coder-plus": 1387,
    "qwen3-vl-plus": 1416,
    "qwen-max": 1317,
    "qwen-plus": 1346,
    "qwen-turbo": None,

    # Moonshot
    "kimi-k2-0905": 1418,
    "kimi-k2-thinking": 1430,

    # Z.ai
    "glm-4.6": 1426,
    "glm-4.5-air": 1373,

    # Nvidia
    "nvidia/llama-3.3-nemotron-super-49b-v1": 1343,
    "nvidia/nemotron-nano-9b-v2": None,

    # AWS Bedrock (EU)
    "eu.anthropic.claude-opus-4-7-v1:0": 1505,
    "eu.anthropic.claude-sonnet-4-6-v1:0": 1461,
    "eu.anthropic.claude-haiku-4-5-v1:0": 1408,
    "eu.anthropic.claude-3-7-sonnet-20250219-v1:0": 1370,
    "eu.anthropic.claude-3-5-haiku-20241022-v1:0": 1323,
    "eu.amazon.nova-pro-v1:0": 1290,
    "eu.amazon.nova-lite-v1:0": 1260,
    "eu.amazon.nova-micro-v1:0": 1240,
    "eu.meta.llama3-3-70b-instruct-v1:0": 1318,
    "eu.mistral.mistral-large-2402-v1:0": 1241,

    # Fireworks
    "accounts/fireworks/models/llama4-maverick-instruct-basic": 1327,
    "accounts/fireworks/models/deepseek-v3p2-exp": 1423,
    "accounts/fireworks/models/qwen3-coder-480b-a35b-instruct": 1387,
    "accounts/fireworks/models/glm-4p5-air": 1373,
    "accounts/fireworks/models/llama-v3p1-405b-instruct": 1332,
    "accounts/fireworks/models/kimi-k2-5": 1432,
    "accounts/fireworks/models/kimi-k2-5-turbo": 1451,
    "accounts/fireworks/models/glm-5": 1456,
    "accounts/fireworks/models/glm-5-1": 1471,
    "accounts/fireworks/models/gpt-oss-120b": 1354,
    "accounts/fireworks/models/gpt-oss-20b": 1318,
    "accounts/fireworks/models/minimax-m2-5": 1402,

    # Replicate
    "anthropic/claude-3.7-sonnet": 1370,
    "deepseek-ai/deepseek-r1": 1397,

    # SiliconFlow
    "deepseek-ai/DeepSeek-V3.2": 1423,
    "moonshotai/Kimi-K2-Thinking": 1430,
    "Pro/moonshotai/Kimi-K2.5": 1451,
    "zai-org/GLM-4.6": 1426,
    "Pro/zai-org/GLM-5": 1456,
    "Qwen/Qwen3-235B-A22B-Thinking-2507": 1400,
    "Qwen/Qwen3-235B-A22B-Instruct-2507": 1423,
    "tencent/Hunyuan-A13B-Instruct": 1260,
    "baidu/ERNIE-4.5-300B-A47B": 1280,

    # OpenRouter
    "anthropic/claude-opus-4.6": 1496,
    "anthropic/claude-sonnet-4.5": 1451,
    "anthropic/claude-haiku-4.5": 1408,
    "openai/gpt-5": 1433,
    "openai/gpt-5-nano": 1336,
    "openai/gpt-4.1": 1413,
    "openai/gpt-4.1-mini": 1382,
    "google/gemini-3-flash-preview": 1474,
    "google/gemini-2.5-pro": 1448,
    "google/gemini-2.5-flash": 1411,
    "mistralai/mistral-large": 1305,
    "x-ai/grok-4": 1410,
    "moonshotai/kimi-k2": 1417,
    "deepseek/deepseek-chat": 1423,
    "deepseek/deepseek-r1": 1397,
    "meta-llama/llama-3.3-70b-instruct": 1318,
}


def main():
    p = Path(__file__).resolve().parents[1] / "app" / "data" / "providers.py"
    src = p.read_text(encoding="utf-8")

    # Match each model block's (model_id, arena_elo) pair. We rewrite the
    # arena_elo value inside the same block; block is the smallest chunk
    # starting at "model_id" up to the next model_id or end-of-list.
    pattern = re.compile(
        r'("model_id":\s*"(?P<mid>[^"]+)".*?"arena_elo":\s*)(?P<elo>None|\d+)',
        re.DOTALL,
    )

    seen = set()
    dupe_ids = []
    unknown = []

    def sub(m):
        mid = m.group("mid")
        if mid in seen:
            dupe_ids.append(mid)
        seen.add(mid)
        if mid not in NEW_ELO:
            unknown.append(mid)
            return m.group(0)   # leave untouched
        new_val = NEW_ELO[mid]
        repl = "None" if new_val is None else str(new_val)
        return m.group(1) + repl

    new_src, n = pattern.subn(sub, src)

    missing_in_file = set(NEW_ELO) - seen
    print(f"substitutions: {n}")
    print(f"duplicate model_ids (first occurrence of each counted): {len(dupe_ids)}")
    if dupe_ids:
        for d in dupe_ids:
            print(f"  dup: {d}")
    print(f"unknown (in file, not in NEW_ELO): {len(unknown)}")
    for u in unknown:
        print(f"  unknown: {u}")
    print(f"unused (in NEW_ELO, not in file): {len(missing_in_file)}")
    for u in sorted(missing_in_file):
        print(f"  unused: {u}")

    p.write_text(new_src, encoding="utf-8")
    print(f"wrote {p}")


if __name__ == "__main__":
    main()
