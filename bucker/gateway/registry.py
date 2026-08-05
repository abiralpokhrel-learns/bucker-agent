"""Model registry — capability metadata, not a list of names (spec §5-6).

Model identity is SEPARATE from provider identity: a registry entry carries
its canonical id ("provider/model_id"), its provider, the provider-specific
deployment id, capabilities, pricing, free-tier status, and priority. The
routing engine answers questions like "which currently healthy models
support tool calling with >=32k context?" against this, instead of
hard-coding model names anywhere.

The registry is seeded from the curated ``bucker.models.CATALOG`` plus the
configured chain (BUCKER_MODEL / BUCKER_MODEL_FALLBACKS — the model name
stays in config, never in code; CI greps for hardcoded gpt-/claude-/o*
names and this file keeps every id provider-prefixed). Models configured
but unknown to the catalog are auto-registered with conservative
capabilities and unknown pricing, so a custom chain still routes.

Pricing is approximate metadata for RANKING and cost tracking only. It is
never a billing source of truth. Unknown price => cost_usd stays None
(unknown, never fabricated as 0 — same rule as the router).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bucker.config import settings
from bucker.gateway.models import (
    CAP_CODING,
    CAP_REASONING,
    CAP_STREAMING,
    CAP_STRUCTURED,
    CAP_TOOLS,
    CAP_VISION,
)

#: Provider prefixes the gateway knows how to serve today. Order matters
#: only for display; routing order comes from priority.
KNOWN_PROVIDERS = ("deepseek", "openrouter", "ollama")

#: USD per 1M tokens (input, output). Approximate, for ranking/cost only.
_PRICE_PER_M: dict[str, tuple[float, float]] = {
    "deepseek/deepseek-v4-flash": (0.28, 0.42),
    "deepseek/deepseek-v3": (0.27, 1.10),
    "openrouter/anthropic/claude-sonnet-4.5": (3.00, 15.00),
    "openrouter/anthropic/claude-haiku-4.5": (1.00, 5.00),
    "openrouter/openai/gpt-4o-mini": (0.15, 0.60),
}

#: Extra capabilities beyond the default (tools + streaming + coding).
_VISION_MODELS = frozenset({"google/gemini-2.5-flash:free"})
_REASONING_MODELS = frozenset(
    {"deepseek/deepseek-v4-flash", "ollama/deepseek-r1:7b"}
)
_STRUCTURED_MODELS = frozenset(
    {
        "deepseek/deepseek-v4-flash",
        "deepseek/deepseek-v3",
        "openrouter/openai/gpt-4o-mini",
        "openrouter/anthropic/claude-sonnet-4.5",
        "openrouter/anthropic/claude-haiku-4.5",
    }
)

#: Max output tokens, per canonical id (default 8192).
_MAX_OUTPUT: dict[str, int] = {
    "openrouter/anthropic/claude-sonnet-4.5": 64_000,
    "openrouter/anthropic/claude-haiku-4.5": 64_000,
}

_DEFAULT_CAPABILITIES = frozenset({CAP_TOOLS, CAP_STREAMING, CAP_CODING})


@dataclass(frozen=True, slots=True)
class GatewayModel:
    """One model the gateway can route to (spec §5)."""

    canonical_id: str            # "deepseek/deepseek-v4-flash"
    provider: str                # "deepseek" | "openrouter" | "ollama" | ...
    provider_model_id: str       # id sent to the provider API
    family: str                  # e.g. "qwen2.5-coder"
    context: int                 # context window in tokens
    max_output: int              # maximum output tokens
    capabilities: frozenset[str]
    price_input_per_m: float | None   # USD per 1M input tokens
    price_output_per_m: float | None  # USD per 1M output tokens
    free: bool = False           # legitimately free tier (zero cost)
    priority: int = 100          # lower = tried first by "priority" policy
    status: str = "available"    # "available" | "disabled" (operator gate)
    daily_limit: int | None = None    # documented free-tier daily cap
    notes: str = ""

    @property
    def key(self) -> str:
        """Circuit-breaker / stats key for this deployment."""
        return f"{self.provider}/{self.provider_model_id}"

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    def price_unknown(self) -> bool:
        return self.price_input_per_m is None or self.price_output_per_m is None


class ModelRegistry:
    """Capability-queryable registry (spec §5). Static by design — provider
    health, quota, and credential availability are checked by the routing
    engine at request time, not frozen here."""

    def __init__(self, models: dict[str, GatewayModel] | None = None) -> None:
        self._models: dict[str, GatewayModel] = dict(models or {})

    # ------------------------------------------------------------- build --
    @classmethod
    def default(cls) -> ModelRegistry:
        """Registry seeded from the catalog + configured chain."""
        models: dict[str, GatewayModel] = {}

        # Curated catalog first: its metadata (context, tier, daily caps)
        # applies to chain entries too, when the ids match.
        try:
            from bucker.models import CATALOG

            catalog_by_id = {entry.id: entry for entry in CATALOG}
        except ImportError:  # pragma: no cover — catalog always exists
            catalog_by_id = {}

        # Chain order: the configured primary and its fallbacks are what the
        # operator chose; "priority" routing honors that order.
        chain = [m for m in (settings.model, *settings.model_fallbacks) if m]
        for i, cid in enumerate(chain):
            entry = catalog_by_id.get(cid)
            if entry is not None:
                is_free = entry.tier in ("free", "local")
                models[cid] = _build_model(
                    cid,
                    context=entry.context,
                    free=is_free,
                    price=(0.0, 0.0) if is_free else None,
                    daily_limit=entry.daily_limit,
                    notes=entry.notes,
                    priority=i,
                )
            else:
                models[cid] = _replace(_build_model(cid), priority=i)

        # Then the remaining catalog, for models not already in the chain.
        for i, (cid, entry) in enumerate(catalog_by_id.items()):
            if cid in models:
                continue
            is_free = entry.tier in ("free", "local")
            models[cid] = _build_model(
                cid,
                context=entry.context,
                free=is_free,
                price=(0.0, 0.0) if is_free else None,
                daily_limit=entry.daily_limit,
                notes=entry.notes,
                priority=100 + i,
            )

        return cls(models)

    # ------------------------------------------------------------ query --
    def get(self, canonical_id: str) -> GatewayModel | None:
        return self._models.get(canonical_id)

    def all(self) -> list[GatewayModel]:
        return sorted(self._models.values(), key=lambda m: m.priority)

    def available(self) -> list[GatewayModel]:
        return [m for m in self.all() if m.status == "available"]

    def filter(
        self,
        *,
        needs_tools: bool = False,
        needs_streaming: bool = False,
        needs_vision: bool = False,
        needs_reasoning: bool = False,
        min_context: int = 0,
        free_only: bool = False,
        max_cost_usd: float | None = None,
    ) -> list[GatewayModel]:
        """Pure capability/constraint filter — no I/O (spec §7 hard pass)."""
        out = []
        for model in self.available():
            if needs_tools and not model.supports(CAP_TOOLS):
                continue
            if needs_streaming and not model.supports(CAP_STREAMING):
                continue
            if needs_vision and not model.supports(CAP_VISION):
                continue
            if needs_reasoning and not model.supports(CAP_REASONING):
                continue
            if model.context < min_context:
                continue
            if free_only and not model.free:
                continue
            if max_cost_usd is not None and not model.price_unknown():
                total = model.price_input_per_m + model.price_output_per_m
                if total > max_cost_usd:
                    continue
            out.append(model)
        return out


# --------------------------------------------------------------------------
# Construction helpers
# --------------------------------------------------------------------------

def _build_model(
    canonical_id: str,
    *,
    context: int = 128_000,
    free: bool = False,
    price: tuple[float, float] | None = None,
    daily_limit: int | None = None,
    notes: str = "",
    priority: int = 100,
) -> GatewayModel:
    provider, sep, rest = canonical_id.partition("/")
    if not sep:
        # Bare id (no provider prefix): assume a direct OpenAI-compatible
        # endpoint. Only routable if an adapter for "openai" exists.
        provider, rest = "openai", canonical_id

    capabilities = _DEFAULT_CAPABILITIES
    if canonical_id in _VISION_MODELS:
        capabilities = capabilities | {CAP_VISION}
    if canonical_id in _REASONING_MODELS:
        capabilities = capabilities | {CAP_REASONING}
    if canonical_id in _STRUCTURED_MODELS:
        capabilities = capabilities | {CAP_STRUCTURED}

    if price is None:
        price = _PRICE_PER_M.get(canonical_id)

    return GatewayModel(
        canonical_id=canonical_id,
        provider=provider,
        provider_model_id=rest,
        family=rest.split("/")[0].split(":")[0],
        context=context,
        max_output=_MAX_OUTPUT.get(canonical_id, 8192),
        capabilities=capabilities,
        price_input_per_m=price[0] if price else None,
        price_output_per_m=price[1] if price else None,
        free=free,
        priority=priority,
        daily_limit=daily_limit,
        notes=notes,
    )


def _replace(model: GatewayModel, **changes: Any) -> GatewayModel:
    return GatewayModel(**{**model.__dict__, **changes})
