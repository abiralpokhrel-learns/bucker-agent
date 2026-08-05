"""Inference gateway — the AI infrastructure control plane (spec §1).

Hermes/agents own agent behavior; this package owns model infrastructure:
provider abstraction, model registry, policy routing, fallback, retries,
circuit breakers, quotas, and normalized errors. It is deliberately
provider-neutral — the provider adapters (``adapters.py``) are the only
place canonical objects meet a provider's API.

Layout:

    models.py    canonical InferenceRequest / InferenceResponse / stream events
    errors.py    normalized error taxonomy (drives routing decisions)
    registry.py  model registry (capabilities, pricing, free tier)
    adapters.py  ProviderAdapter interface + DeepSeek/OpenRouter/Ollama
                 + SimulatedProvider (hermetic tests)
    circuit.py   circuit breakers + rolling health stats
    quota.py     usage ledger (Postgres) + daily entitlement checks
    routing.py   RouterEngine: plan -> filter -> rank -> execute
"""

from bucker.gateway.routing import RouterEngine, RoutingDecision

__all__ = ["RouterEngine", "RoutingDecision"]
