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
    daily_limit: int | None = None  # documented free-tier daily cap (free only)


def _m(*, provider: str, tier: str, name: str, context: int,
       model_id: str, notes: str = "", daily_limit: int | None = None) -> CatalogModel:
    return CatalogModel(
        id=f"{provider}/{model_id}",
        provider=provider,
        tier=tier,
        name=name,
        context=context,
        notes=notes,
        daily_limit=daily_limit,
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
       notes="current best free hosted coder; 0.0 per 1M tokens",
       daily_limit=50),
    _m(provider="openrouter", tier="free", name="DeepSeek V3 (free)",
       context=131_072, model_id="deepseek/deepseek-v3:free",
       notes="free tier of DeepSeek V3 when available",
       daily_limit=50),
    _m(provider="openrouter", tier="free", name="Llama 3.3 70B (free)",
       context=131_072, model_id="meta-llama/llama-3.3-70b-instruct:free",
       notes="free tier of the 70B instruct model",
       daily_limit=50),
    _m(provider="openrouter", tier="free", name="Qwen 2.5 72B (free)",
       context=131_072, model_id="qwen/qwen-2.5-72b-instruct:free",
       notes="free tier of the 72B instruct model",
       daily_limit=50),
    _m(provider="openrouter", tier="free", name="Gemini 2.5 Flash (free)",
       context=1_000_000, model_id="google/gemini-2.5-flash:free",
       notes="free tier of Gemini 2.5 Flash; subject to availability",
       daily_limit=50),
    # ------------------------------------------------------------- paid (DeepSeek)
    _m(provider="deepseek", tier="paid", name="DeepSeek V4 Flash",
       context=128_000, model_id="deepseek-v4-flash",
       notes="official DeepSeek API, OpenAI-compatible; cheap per token"),
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
    "deepseek": {
        "name": "DeepSeek",
        "env_key": "DEEPSEEK_API_KEY",
        "base_url": "https://api.deepseek.com",
        "tier": "paid",
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


def free_tier_rows(today_counts: dict[str, int]) -> list[dict]:
    """The free-tier quota panel (OmniRoute-style, honest).

    Pure: given {model_id: calls_today} from telemetry, compute per free
    model the documented daily limit and the remaining estimate. Only
    catalogued free models with a documented limit are shown — an
    unknown free model has no claim we can make about its quota.
    """
    rows = []
    for m in CATALOG:
        if m.tier != "free" or not m.daily_limit:
            continue
        calls = int(today_counts.get(m.id, 0))
        rows.append({
            "model": m.id,
            "name": m.name,
            "calls_today": calls,
            "limit": m.daily_limit,
            "remaining": max(m.daily_limit - calls, 0),
            "pct": min(calls / m.daily_limit * 100, 100.0),
        })
    rows.sort(key=lambda r: -r["calls_today"])
    return rows


def list_by_tier(tier: str) -> list[CatalogModel]:
    return [m for m in CATALOG if m.tier == tier]


def suggest_chain(
    *,
    ollama_models: list[str],
    openrouter_key_ok: bool,
    deepseek_key_ok: bool = False,
) -> list[str]:
    """Propose a deterministic, free-first fallback chain.

    The chain is a CONFIGURATION suggestion — the router never reorders it
    at runtime (replay determinism is keyed to the primary). This is what
    the setup wizard writes into BUCKER_MODEL + BUCKER_MODEL_FALLBACKS.

    Ordering: best local coder (if installed) → best FREE hosted OpenRouter
    model (if a key works — paid OpenRouter models are never suggested) →
    DeepSeek V4 Flash (if a key works). Always exactly the models that can
    actually serve today.
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

    # 2. FREE hosted OpenRouter only (needs a working key). Paid OpenRouter
    #    models are intentionally never suggested — free tier first, always.
    if openrouter_key_ok:
        chain.append("openrouter/nvidia/nemotron-3-super-120b-a12b:free")

    # 3. DeepSeek V4 Flash (paid, needs its own key).
    if deepseek_key_ok:
        chain.append("deepseek/deepseek-v4-flash")

    return chain
