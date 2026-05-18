"""One-shot migration: rewrite aa_index values in app/data/providers.py to
match the current Artificial Analysis Intelligence Index (post-rebase, 0–60
scale) as scraped from https://artificialanalysis.ai/models.

Strategy
--------
1. Hard-coded MAPPING keyed by (provider, model_id) → new_aa value.
2. Where AA has multiple scores per model (reasoning effort levels), we use
   the xhigh/max value for reasoning/full variants and medium for "mini"/
   "nano" sub-variants.
3. Models absent from AA are estimated by inference from:
   - adjacent AA-scored models in the same family,
   - relative old-scale positioning,
   - arena_elo ranking,
   - coder/reasoning vs general-purpose class priors.
   Each estimate is marked in the comment column of this file so it can be
   refined later.
4. The file is rewritten in-place using a line-level regex pass; formatting
   and comments elsewhere in providers.py are preserved.

Run:
    python -m tools.migrate_aa_index
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROVIDERS_PATH = ROOT / "app" / "data" / "providers.py"

# (provider, model_id) → new aa_index value.  None = leave null / fall back
# to quality_score.  Comments inline mark AA-scraped ("AA") vs estimates.
MAPPING: dict[tuple[str, str], float | None] = {
    # ── OpenAI ─────────────────────────────────────────────────────────────
    ("OpenAI", "gpt-4.1"):             28,   # est (pre-GPT-5 era, below o3=38)
    ("OpenAI", "gpt-4.1-mini"):        22,   # est
    ("OpenAI", "gpt-4.1-nano"):        16,   # est
    ("OpenAI", "gpt-4o"):              25,   # est
    ("OpenAI", "gpt-4o-mini"):         18,   # est
    ("OpenAI", "o1"):                  40,   # est (between o3=38 and newer)
    ("OpenAI", "o1-mini"):             25,   # est
    ("OpenAI", "o3"):                  38,   # AA
    ("OpenAI", "o3-mini"):             30,   # est
    ("OpenAI", "o4-mini"):             36,   # est (just below o3)
    ("OpenAI", "gpt-5.4"):             57,   # AA xhigh
    ("OpenAI", "gpt-5.4-mini"):        38,   # AA medium
    ("OpenAI", "gpt-5.4-nano"):        38,   # AA default
    ("OpenAI", "gpt-5.4-pro"):         60,   # est (above 5.4 xhigh, ≤ 5.5 xhigh)
    ("OpenAI", "gpt-5.3-chat-latest"): 50,   # est (near 5.3 Codex=54)
    ("OpenAI", "gpt-5.3-codex"):       54,   # AA xhigh
    ("OpenAI", "o3-deep-research"):    42,   # est (o3+search)
    ("OpenAI", "o4-mini-deep-research"): 33, # est
    ("OpenAI", "gpt-5"):               45,   # est (legacy GPT-5)
    ("OpenAI", "gpt-5-mini"):          30,   # est
    ("OpenAI", "gpt-5-nano"):          20,   # est
    ("OpenAI", "gpt-5-pro"):           50,   # est
    ("OpenAI", "o3-pro"):              45,   # est (above o3)
    ("OpenAI", "text-embedding-3-large"): None,
    ("OpenAI", "text-embedding-3-small"): None,

    # ── Anthropic ──────────────────────────────────────────────────────────
    ("Anthropic", "claude-opus-4-7"):   57,  # AA max
    ("Anthropic", "claude-sonnet-4-6"): 52,  # AA max
    ("Anthropic", "claude-opus-4-5"):   54,  # est (below 4.7)
    ("Anthropic", "claude-sonnet-4-5"): 50,  # est (below 4.6)
    ("Anthropic", "claude-3-7-sonnet-20250219"): 32, # est
    ("Anthropic", "claude-3-5-sonnet-20241022"): 30, # est
    ("Anthropic", "claude-haiku-4-5"):  37,  # AA
    ("Anthropic", "claude-3-5-haiku-20241022"): 20, # est
    ("Anthropic", "claude-3-opus-20240229"):    25, # est (legacy)

    # ── Google ─────────────────────────────────────────────────────────────
    ("Google", "gemini-3.1-pro-preview"):       57,  # AA
    ("Google", "gemini-3-flash-preview"):       46,  # AA max
    ("Google", "gemini-3.1-flash-lite-preview"): 34, # AA
    ("Google", "gemini-2.5-pro"):               35,  # AA
    ("Google", "gemini-2.5-flash"):             28,  # est (above Flash-Lite 22)
    ("Google", "gemini-2.5-flash-lite"):        22,  # AA
    ("Google", "gemini-2.0-flash"):             22,  # est
    ("Google", "gemini-2.0-flash-lite"):        17,  # est
    ("Google", "gemini-1.5-pro"):               26,  # est
    ("Google", "gemini-1.5-flash"):             22,  # est
    ("Google", "gemini-1.5-flash-8b"):          15,  # est
    ("Google", "gemini-embedding-001"):         None,
    ("Google", "text-embedding-004"):           None,

    # ── Mistral AI ─────────────────────────────────────────────────────────
    ("Mistral AI", "mistral-large-latest"):     20,  # est (Large 2, below Large 3=23)
    ("Mistral AI", "mistral-small-latest"):     18,  # est (Small 3.1, below Small 4=19/28)
    ("Mistral AI", "mistral-medium-latest"):    21,  # AA Medium 3.1
    ("Mistral AI", "mistral-large-3"):          23,  # AA
    ("Mistral AI", "magistral-medium-latest"):  27,  # AA Medium 1.2
    ("Mistral AI", "magistral-small-latest"):   18,  # AA Small 1.2
    ("Mistral AI", "devstral-medium-latest"):   22,  # AA Devstral 2
    ("Mistral AI", "ministral-8b-latest"):      15,  # AA Ministral 3 8B
    ("Mistral AI", "ministral-3b-latest"):      11,  # AA Ministral 3 3B
    ("Mistral AI", "open-mistral-nemo"):        16,  # est
    ("Mistral AI", "codestral-latest"):         22,  # est (coder tier)
    ("Mistral AI", "pixtral-large-latest"):     20,  # est (Large 2 + vision)
    ("Mistral AI", "open-mixtral-8x22b"):       17,  # est (older MoE)
    ("Mistral AI", "mistral-embed"):            None,

    # ── Meta (API) ─────────────────────────────────────────────────────────
    ("Meta (API)", "meta-llama/Llama-4-Maverick-17B-128E-Instruct"): 18,  # AA
    ("Meta (API)", "meta-llama/Llama-4-Scout-17B-16E-Instruct"):     14,  # AA
    ("Meta (API)", "meta-llama/Llama-4-Behemoth"):                   30,  # est (top-tier Meta)

    # ── DeepSeek ───────────────────────────────────────────────────────────
    ("DeepSeek", "deepseek-chat"):     32,   # AA V3.2 non-reasoning
    ("DeepSeek", "deepseek-reasoner"): 42,   # AA V3.2 reasoning

    # ── Groq (wrappers — inherit underlying) ───────────────────────────────
    ("Groq", "meta-llama/llama-4-maverick-17b-128e-instruct"): 18,  # AA
    ("Groq", "meta-llama/llama-4-scout-17b-16e-instruct"):     14,  # AA
    ("Groq", "llama-3.3-70b-versatile"):  14,  # AA Llama 3.3 70B
    ("Groq", "llama-3.1-8b-instant"):      8,  # est
    ("Groq", "deepseek-r1-distill-llama-70b"): 16,  # AA
    ("Groq", "qwen-2.5-72b-instruct"):    16,  # est (pre-Qwen3)
    ("Groq", "gemma2-9b-it"):             15,  # est (older than Gemma 4 E4B=19)
    ("Groq", "openai/gpt-oss-120b"):      33,  # AA high
    ("Groq", "openai/gpt-oss-20b"):       24,  # AA high
    ("Groq", "qwen/qwen3-32b"):           28,  # est (between 3.5 35B=31, 9B=32)
    ("Groq", "moonshotai/kimi-k2-instruct-0905"): 32,  # est (K2 older than K2.5=37)

    # ── Cerebras ───────────────────────────────────────────────────────────
    ("Cerebras", "llama-3.3-70b"):        14,  # AA
    ("Cerebras", "llama3.1-8b"):           8,  # est

    # ── Together AI (wrappers — inherit underlying) ────────────────────────
    ("Together AI", "meta-llama/Llama-4-Maverick-17B-128E-Instruct-Turbo"): 18,  # AA
    ("Together AI", "meta-llama/Llama-3.3-70B-Instruct-Turbo"):             14,  # AA
    ("Together AI", "meta-llama/Meta-Llama-3.1-405B-Instruct-Turbo"):       17,  # AA
    ("Together AI", "deepseek-ai/DeepSeek-V3"):                             25,  # est (pre-V3.2)
    ("Together AI", "Qwen/Qwen2.5-72B-Instruct-Turbo"):                     16,  # est
    ("Together AI", "deepseek-ai/DeepSeek-V3.1"):                           28,  # est
    ("Together AI", "deepseek-ai/DeepSeek-R1"):                             27,  # AA R1 0528
    ("Together AI", "Qwen/Qwen3-Coder-480B-A35B-Instruct"):                 40,  # est (Qwen3 coder, pre-3.6)
    ("Together AI", "moonshotai/Kimi-K2-Instruct"):                         32,  # est (K2 older)
    ("Together AI", "openai/gpt-oss-120b"):                                 33,  # AA
    ("Together AI", "google/gemma-4-31b-it"):                               39,  # AA Gemma 4 31B max
    ("Together AI", "MiniMaxAI/MiniMax-M2.5"):                              42,  # est (M2.7=50 is newer)

    # ── xAI ────────────────────────────────────────────────────────────────
    ("xAI", "grok-4.20-0309-reasoning"):     49,  # AA Grok 4.20 0309 v2 reasoning
    ("xAI", "grok-4.20-0309-non-reasoning"): 29,  # AA non-reasoning
    ("xAI", "grok-4.20-multi-agent-0309"):   50,  # est (above single-agent reasoning)
    ("xAI", "grok-4-1-fast-reasoning"):      39,  # AA Grok 4.1 Fast reasoning
    ("xAI", "grok-4-1-fast-non-reasoning"):  24,  # AA non-reasoning
    ("xAI", "grok-3"):                       28,  # est
    ("xAI", "grok-3-mini"):                  22,  # est (Grok 3 mini Reasoning high=32)
    ("xAI", "grok-2-vision-1212"):           18,  # est

    # ── Cohere ─────────────────────────────────────────────────────────────
    ("Cohere", "command-a-03-2025"):      13,  # AA
    ("Cohere", "command-r-plus-08-2024"): 10,  # est
    ("Cohere", "command-r-08-2024"):       8,  # est
    ("Cohere", "embed-english-v3.0"):     None,
    ("Cohere", "embed-multilingual-v3.0"): None,

    # ── Perplexity ─────────────────────────────────────────────────────────
    ("Perplexity", "sonar-pro"): 20,  # est (GPT-4o-backed + search)
    ("Perplexity", "sonar"):     15,  # est

    # ── Qwen ───────────────────────────────────────────────────────────────
    ("Qwen", "qwen3-max"):         45,  # est (Qwen3.6 Max Preview=52 is newer)
    ("Qwen", "qwen3-235b-a22b"):   38,  # est (between 3.5 122B=42 and 3.5 35B=31)
    ("Qwen", "qwen3-coder-plus"):  40,  # est (coder tier, pre-3.6)
    ("Qwen", "qwen3-vl-plus"):     38,  # est (VL 235B)
    ("Qwen", "qwen-max"):          35,  # est (pre-Qwen3)
    ("Qwen", "qwen-plus"):         25,  # est
    ("Qwen", "qwen-turbo"):        20,  # est

    # ── Moonshot AI ────────────────────────────────────────────────────────
    ("Moonshot AI", "kimi-k2-0905"):   32,  # est (K2 pre-K2.5=37)
    ("Moonshot AI", "kimi-k2-thinking"): 42,  # est (pre-K2.6=54, above K2.5=37)

    # ── Z.ai ───────────────────────────────────────────────────────────────
    ("Z.ai", "glm-4.6"):     36,  # est (pre-GLM-5=41/50)
    ("Z.ai", "glm-4.5-air"): 28,  # est
    ("Z.ai", "glm-5.1"):     51,  # AA GLM-5.1 reasoning
    ("Z.ai", "glm-5"):       50,  # AA GLM-5 reasoning

    # ── NVIDIA ─────────────────────────────────────────────────────────────
    ("NVIDIA", "nvidia/llama-3.3-nemotron-super-49b-v1"): 18,  # AA Nemotron Super 49B
    ("NVIDIA", "nvidia/nemotron-nano-9b-v2"):             15,  # AA

    # ── AWS Bedrock ────────────────────────────────────────────────────────
    ("AWS Bedrock", "eu.anthropic.claude-opus-4-7-v1:0"):            57,  # AA
    ("AWS Bedrock", "eu.anthropic.claude-sonnet-4-6-v1:0"):          52,  # AA
    ("AWS Bedrock", "eu.anthropic.claude-haiku-4-5-v1:0"):           37,  # AA
    ("AWS Bedrock", "eu.anthropic.claude-3-7-sonnet-20250219-v1:0"): 32,  # est
    ("AWS Bedrock", "eu.anthropic.claude-3-5-haiku-20241022-v1:0"):  20,  # est
    ("AWS Bedrock", "eu.amazon.nova-pro-v1:0"):                      22,  # est (v1 pre-v2)
    ("AWS Bedrock", "eu.amazon.nova-lite-v1:0"):                     16,  # est
    ("AWS Bedrock", "eu.amazon.nova-micro-v1:0"):                    10,  # AA Nova Micro
    ("AWS Bedrock", "eu.meta.llama3-3-70b-instruct-v1:0"):           14,  # AA
    ("AWS Bedrock", "eu.mistral.mistral-large-2402-v1:0"):           18,  # est (older Large)

    # ── Azure OpenAI (inherit OpenAI) ──────────────────────────────────────
    ("Azure OpenAI", "gpt-4o"):         25,
    ("Azure OpenAI", "gpt-4o-mini"):    18,
    ("Azure OpenAI", "gpt-5.4"):        57,  # AA xhigh
    ("Azure OpenAI", "gpt-5.4-mini"):   38,  # AA medium
    ("Azure OpenAI", "gpt-5"):          45,
    ("Azure OpenAI", "gpt-5-mini"):     30,
    ("Azure OpenAI", "gpt-4.1"):        28,

    # ── Fireworks AI (wrappers) ────────────────────────────────────────────
    ("Fireworks AI", "accounts/fireworks/models/llama4-maverick-instruct-basic"): 18,  # AA
    ("Fireworks AI", "accounts/fireworks/models/deepseek-v3p2-exp"):              44,  # est
    ("Fireworks AI", "accounts/fireworks/models/qwen3-coder-480b-a35b-instruct"): 40,  # est
    ("Fireworks AI", "accounts/fireworks/models/glm-4p5-air"):                    28,  # est
    ("Fireworks AI", "accounts/fireworks/models/llama-v3p1-405b-instruct"):       17,  # AA
    ("Fireworks AI", "accounts/fireworks/models/kimi-k2-5"):                      37,  # AA Kimi K2.5
    ("Fireworks AI", "accounts/fireworks/models/kimi-k2-5-turbo"):                38,  # est
    ("Fireworks AI", "accounts/fireworks/models/glm-5"):                          50,  # AA
    ("Fireworks AI", "accounts/fireworks/models/glm-5-1"):                        51,  # AA
    ("Fireworks AI", "accounts/fireworks/models/gpt-oss-120b"):                   33,  # AA high
    ("Fireworks AI", "accounts/fireworks/models/gpt-oss-20b"):                    24,  # AA high
    ("Fireworks AI", "accounts/fireworks/models/minimax-m2-5"):                   42,  # est

    # ── Replicate (wrappers) ───────────────────────────────────────────────
    ("Replicate", "anthropic/claude-3.7-sonnet"): 32,  # est
    ("Replicate", "deepseek-ai/deepseek-r1"):     27,  # AA

    # ── SiliconFlow (wrappers) ─────────────────────────────────────────────
    ("SiliconFlow", "deepseek-ai/DeepSeek-V3.2"):            32,  # AA non-reasoning (chat)
    ("SiliconFlow", "deepseek-ai/DeepSeek-R1"):              27,  # AA
    ("SiliconFlow", "moonshotai/Kimi-K2-Thinking"):          42,  # est
    ("SiliconFlow", "Pro/moonshotai/Kimi-K2.5"):             37,  # AA
    ("SiliconFlow", "zai-org/GLM-4.6"):                      36,  # est
    ("SiliconFlow", "Pro/zai-org/GLM-5"):                    50,  # AA
    ("SiliconFlow", "Qwen/Qwen3-Coder-480B-A35B-Instruct"):  40,  # est
    ("SiliconFlow", "Qwen/Qwen3-235B-A22B-Thinking-2507"):   42,  # est (thinking variant)
    ("SiliconFlow", "Qwen/Qwen3-235B-A22B-Instruct-2507"):   35,  # est (instruct variant)
    ("SiliconFlow", "tencent/Hunyuan-A13B-Instruct"):        22,  # est
    ("SiliconFlow", "baidu/ERNIE-4.5-300B-A47B"):            15,  # AA

    # ── OpenRouter (wrappers — inherit underlying) ─────────────────────────
    ("OpenRouter", "anthropic/claude-opus-4.6"):           55,  # est (4.6 just below 4.7=57)
    ("OpenRouter", "anthropic/claude-sonnet-4.5"):         50,  # est
    ("OpenRouter", "anthropic/claude-haiku-4.5"):          37,  # AA
    ("OpenRouter", "openai/gpt-5"):                        45,
    ("OpenRouter", "openai/gpt-5-nano"):                   20,
    ("OpenRouter", "openai/gpt-4.1"):                      28,
    ("OpenRouter", "openai/gpt-4.1-mini"):                 22,
    ("OpenRouter", "google/gemini-3-flash-preview"):       46,  # AA
    ("OpenRouter", "google/gemini-2.5-pro"):               35,  # AA
    ("OpenRouter", "google/gemini-2.5-flash"):             28,
    ("OpenRouter", "mistralai/mistral-large"):             20,
    ("OpenRouter", "x-ai/grok-4"):                         45,  # est (pre-Grok 4.20)
    ("OpenRouter", "moonshotai/kimi-k2"):                  32,
    ("OpenRouter", "deepseek/deepseek-chat"):              32,  # AA V3.2 non-reasoning
    ("OpenRouter", "deepseek/deepseek-r1"):                27,  # AA
    ("OpenRouter", "meta-llama/llama-3.3-70b-instruct"):   14,  # AA

    # ── Aleph Alpha ────────────────────────────────────────────────────────
    ("Aleph Alpha", "luminous-supreme-control"): 16,  # est (older EU-native)
    ("Aleph Alpha", "pharia-1-llm-7b-control"):   8,  # est

    # ── AI21 Labs ──────────────────────────────────────────────────────────
    ("AI21 Labs", "jamba-2-large"): 14,  # est (1.7 Large=11, 2 newer)
    ("AI21 Labs", "jamba-2-mini"):  10,  # est

    # ── ByteDance ──────────────────────────────────────────────────────────
    ("ByteDance", "doubao-2.0-pro"):  36,  # est (Doubao Seed Code=34)
    ("ByteDance", "doubao-2.0-lite"): 20,  # est

    # ── Baidu ──────────────────────────────────────────────────────────────
    ("Baidu", "ernie-5.0"):        28,  # est (ERNIE 5.0 Thinking Preview=29)
    ("Baidu", "ernie-4.5-turbo"):  14,  # est (ERNIE 4.5 300B=15)
}


# ── Rewriter ────────────────────────────────────────────────────────────────

# Match one model dict block.  We assume model_id is on the first line of the
# block (matches the current providers.py style) and aa_index is somewhere
# within the next ~12 lines.
MODEL_BLOCK_RE = re.compile(
    r'("provider":\s*"(?P<provider>[^"]+)",\s*'
    r'"model":\s*"[^"]*",\s*'
    r'"model_id":\s*"(?P<model_id>[^"]+)")'
    r'(?P<body>.*?)'
    r'(?P<aa>"aa_index":\s*)(?P<val>None|[\d.]+)',
    re.DOTALL,
)


def rewrite(text: str) -> tuple[str, int, list[str]]:
    applied = 0
    missing: list[str] = []

    def sub(match: re.Match) -> str:
        nonlocal applied
        provider = match.group("provider")
        model_id = match.group("model_id")
        key = (provider, model_id)
        if key not in MAPPING:
            missing.append(f"{provider} :: {model_id}")
            return match.group(0)  # leave untouched
        new_val = MAPPING[key]
        new_repr = "None" if new_val is None else f"{float(new_val):.1f}"
        applied += 1
        return (
            match.group(1) + match.group("body") + match.group("aa") + new_repr
        )

    new_text = MODEL_BLOCK_RE.sub(sub, text)
    return new_text, applied, missing


def main() -> int:
    text = PROVIDERS_PATH.read_text(encoding="utf-8")
    new_text, applied, missing = rewrite(text)
    if missing:
        print("Models NOT in MAPPING (left untouched):")
        for m in missing:
            print(f"  - {m}")
        print()
    PROVIDERS_PATH.write_text(new_text, encoding="utf-8")
    print(f"Rewrote {applied} aa_index values in {PROVIDERS_PATH.relative_to(ROOT)}.")
    if missing:
        print(f"{len(missing)} models skipped — review and extend MAPPING if needed.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
