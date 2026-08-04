"""Curated model catalog: free, paid, and local models bucker can use.

The OmniRoute-inspired piece, kept honest: a small, maintainable registry
with tier metadata instead of a scraped 500-model list. Every entry is
either

  * "local"  — runs on your machine via Ollama (free, private, zero cost),
  * "free"   — hosted free tier via OpenRouter (zero cost, needs a key),
  * "paid"   — hosted paid model via OpenRouter (needs a key + credit).

The catalog feeds:
  * `bucker models`       — browse what you could use,
  * `bucker setup`        — propose a free-first chain,
  * GET /api/models       — the dashboard's models page,
  * the router's fallback chain (model ids are exactly what goes in
    BUCKER_MODEL / BUCKER_MODEL_FALLBACKS).

Rules:
  - model ids are the EXACT strings the router accepts: "ollama/<name>"
    or "openrouter/<openrouter-model-id>". Changing an id breaks existing
    .env chains, so treat this as append-only.
  - tier is metadata for humans and for the setup wizard's ordering; the
    ROUTER never silently reorders the chain (replay is keyed to the
    primary model — determinism beats cleverness).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CatalogModel:
    """One known model in the catalog."""

    id: str                     # router id: "ollama/qwen2.5-coder:7b"
    provider: str               # "ollama" | "openrouter"
    tier: str                   # "local" | "free" | "paid"
    name: str                   # human name, e.g. "Qwen 2.5 Coder 7B"
    context: int                # context window in tokens
    notes: str = ""             # one-line honest note


def _m(*, provider: str, tier: str, name: str, context: int,
       model_id: str, notes: str = "") -> CatalogModel:
    return CatalogModel(
        id=f"{provider}/{model_id}",
        provider=provider,
        tier=tier,
        name=name,
        context=context,
        notes=notes,
    )


#: The registry. Order = display order (local first, then free, then paid).
CATALOG: tuple[CatalogModel, ...] = (
    # ------------------------------------------------------------ local (Ollama)
    _m(provider="ollama", tier="local", name="Qwen 2.5 Coder 7B",
       context=32_768, model_id="qwen2.5-coder:7b",
       notes="best free local coder; needs ~8 GB RAM"),
    _m(provider="ollama", tier="local", name="Qwen 2.5 Coder 3B",
       context=32_768, model_id="qwen2.5-coder:3b",
       notes="fast local fallback; weaker diffs"),
    _m(provider="ollama", tier="local", name="Llama 3.2 3B",
       context=128_000, model_id="llama3.2:3b",
       notes="general-purpose local; no code focus"),
    _m(provider="ollama", tier="local", name="DeepSeek R1 7B (distill)",
       context=16_384, model_id="deepseek-r1:7b",
       notes="reasoning-style local model"),
    # ------------------------------------------------------------- free (OpenRouter)
    _m(provider="openrouter", tier="free", name="Nemotron 3 Super 120B (free)",
       context=131_072, model_id="nvidia/nemotron-3-super-120b-a12b:free",
       notes="current best free hosted coder; 0.0 per 1M tokens"),
    _m(provider="openrouter", tier="free", name="DeepSeek V3 (free)",
       context=131_072, model_id="deepseek/deepseek-v3:free",
       notes="free tier of DeepSeek V3 when available"),
    _m(provider="openrouter", tier="free", name="Llama 3.3 70B (free)",
       context=131_072, model_id="meta-llama/llama-3.3-70b-instruct:free",
       notes="free tier of the 70B instruct model"),
    _m(provider="openrouter", tier="free", name="Qwen 2.5 72B (free)",
       context=131_072, model_id="qwen/qwen-2.5-72b-instruct:free",
       notes="free tier of the 72B instruct model"),
    _m(provider="openrouter", tier="free", name="Gemini 2.5 Flash (free)",
       context=1_000_000, model_id="google/gemini-2.5-flash:free",
       notes="free tier of Gemini 2.5 Flash; subject to availability"),
    # -------------------------------------------------------------- paid (OpenRouter)
    _m(provider="openrouter", tier="paid", name="Claude Sonnet 4.5",
       context=200_000, model_id="anthropic/claude-sonnet-4.5",
       notes="strong all-rounder; the paid default"),
    _m(provider="openrouter", tier="paid", name="Claude Haiku 4.5",
       context=200_000, model_id="anthropic/claude-haiku-4.5",
       notes="cheap and fast for verification-heavy work"),
    _m(provider="openrouter", tier="paid", name="GPT-4o mini",
       context=128_000, model_id="openai/gpt-4o-mini",
       notes="cheap paid default"),
    _m(provider="openrouter", tier="paid", name="DeepSeek V3",
       context=131_072, model_id="deepseek/deepseek-v3",
       notes="very cheap per token"),
)

#: Provider → (env key, human name).
PROVIDERS: dict[str, dict] = {
    "ollama": {
        "name": "Ollama (local)",
        "env_key": None,                       # no key needed
        "base_url": "http://127.0.0.1:11434",
        "tier": "local",
    },
    "openrouter": {
        "name": "OpenRouter",
        "env_key": "OPENROUTER_API_KEY",
        "base_url": "https://openrouter.ai/api/v1",
        "tier": "free+paid",
    },
}


def by_id(model_id: str) -> CatalogModel | None:
    """Catalog entry for a router model id, or None."""
    for m in CATALOG:
        if m.id == model_id:
            return m
    return None


def tier_of(model_id: str) -> str:
    """'local' | 'free' | 'paid' | 'unknown' for any router model id."""
    m = by_id(model_id)
    if m is not None:
        return m.tier
    if model_id.startswith("ollama/"):
        return "local"      # any local model is local, catalogued or not
    return "unknown"


def provider_of(model_id: str) -> str:
    return model_id.split("/", 1)[0] if "/" in model_id else "unknown"


def list_by_tier(tier: str) -> list[CatalogModel]:
    return [m for m in CATALOG if m.tier == tier]


def suggest_chain(
    *,
    ollama_models: list[str],
    openrouter_key_ok: bool,
) -> list[str]:
    """Propose a deterministic, free-first fallback chain.

    The chain is a CONFIGURATION suggestion — the router never reorders it
    at runtime (replay determinism is keyed to the primary). This is what
    the setup wizard writes into BUCKER_MODEL + BUCKER_MODEL_FALLBACKS.

    Ordering: best local coder (if installed) → best free hosted (if a key
    works) → best paid (if a key works). Always exactly the models that
    can actually serve today.
    """
    chain: list[str] = []

    # 1. Local coder models, best first, only if actually pulled.
    local_order = ("qwen2.5-coder:7b", "qwen2.5-coder:3b",
                   "deepseek-r1:7b", "llama3.2:3b")
    for name in local_order:
        if name in ollama_models:
            chain.append(f"ollama/{name}")
            break
    if not chain and ollama_models:
        # Any other local model the user already has.
        chain.append(f"ollama/{ollama_models[0]}")

    # 2. Free hosted (needs a working OpenRouter key).
    if openrouter_key_ok:
        chain.append("openrouter/nvidia/nemotron-3-super-120b-a12b:free")

    # 3. Paid (needs a key; credit is the user's problem to check).
    if openrouter_key_ok:
        chain.append("openrouter/anthropic/claude-sonnet-4.5")

    return chain
