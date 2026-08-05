"""Routing engine proofs — hermetic, no network, no database (spec §48).

The whole point of the gateway is provable behavior when providers fail.
These tests use ``SimulatedProvider`` scripts to prove the exact routing
contracts:

    "If provider A fails with 429, provider B is selected."
    "If provider A has no remaining quota, it is excluded."
    "If all providers fail, the gateway returns a normalized failure."
    "If the local model is available, it becomes the final fallback."
    ... plus tool-call preservation, streaming normalization, error
    taxonomy, circuit breakers, and deadline handling.
"""

from __future__ import annotations

import asyncio

import pytest

from bucker.gateway.adapters import SimulatedProvider
from bucker.gateway.circuit import CircuitRegistry
from bucker.gateway.errors import (
    AllProvidersFailedError,
    InvalidRequestError,
    NoCandidatesError,
)
from bucker.gateway.models import InferenceRequest
from bucker.gateway.quota import QuotaManager
from bucker.gateway.registry import GatewayModel, ModelRegistry
from bucker.gateway.routing import RouterEngine

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

def _model(
    canonical_id: str,
    *,
    provider: str | None = None,
    priority: int = 100,
    free: bool = False,
    price: tuple[float, float] | None = None,
    context: int = 128_000,
    capabilities: frozenset[str] | None = None,
    daily_limit: int | None = None,
) -> GatewayModel:
    p = provider or canonical_id.split("/")[0]
    rest = canonical_id.split("/", 1)[1]
    return GatewayModel(
        canonical_id=canonical_id,
        provider=p,
        provider_model_id=rest,
        family=rest,
        context=context,
        max_output=8192,
        capabilities=capabilities or frozenset({"tools", "streaming", "coding"}),
        price_input_per_m=price[0] if price else None,
        price_output_per_m=price[1] if price else None,
        free=free,
        priority=priority,
        daily_limit=daily_limit,
    )


def _registry(*models: GatewayModel) -> ModelRegistry:
    return ModelRegistry({m.canonical_id: m for m in models})


def _engine(
    *models: GatewayModel,
    policy: str = "priority",
    max_retries: int = 1,
    adapters: dict | None = None,
    quota: QuotaManager | None = None,
    circuits: CircuitRegistry | None = None,
    deadline_s: float = 30.0,
    timeout_s: float = 5.0,
) -> RouterEngine:
    return RouterEngine(
        registry=_registry(*models),
        adapters=adapters,
        quota=quota or QuotaManager(),  # no pool => no quota constraints
        circuits=circuits,
        policy=policy,
        max_retries=max_retries,
        deadline_s=deadline_s,
        timeout_s=timeout_s,
    )


def _req(**kwargs) -> InferenceRequest:
    defaults = {"messages": [{"role": "user", "content": "hi"}]}
    defaults.update(kwargs)
    return InferenceRequest(**defaults)


@pytest.fixture
def sim() -> dict[str, SimulatedProvider]:
    return {
        "a": SimulatedProvider("a"),
        "b": SimulatedProvider("b"),
    }


# --------------------------------------------------------------------------
# 1. Basic routing
# --------------------------------------------------------------------------

async def test_priority_routing_prefers_configured_order(sim):
    engine = _engine(
        _model("a/model-1", provider="a", priority=0),
        _model("b/model-2", provider="b", priority=1),
        adapters=sim,
    )
    resp = await engine.complete(_req())
    assert resp.model == "a/model-1"
    assert resp.provider == "a"
    assert resp.content == "hello from a/model-1"
    assert resp.attempts == 1
    assert resp.from_fallback is False
    assert resp.usage["cost_usd"] is None  # price unknown -> cost unknown


async def test_explicit_model_preference_goes_first(sim):
    engine = _engine(
        _model("a/model-1", provider="a", priority=0),
        _model("b/model-2", provider="b", priority=1),
        adapters=sim,
    )
    resp = await engine.complete(_req(model="b/model-2"))
    assert resp.model == "b/model-2"


async def test_unknown_model_rejected_early(sim):
    engine = _engine(_model("a/model-1", provider="a"), adapters=sim)
    with pytest.raises(InvalidRequestError):
        await engine.complete(_req(model="nope/does-not-exist"))


async def test_explicit_model_failing_hard_requirements_rejected(sim):
    engine = _engine(
        _model("a/model-1", provider="a", free=False),
        adapters=sim,
    )
    with pytest.raises(InvalidRequestError):
        await engine.complete(_req(model="a/model-1", free_only=True))


async def test_no_candidates_raises_early(sim):
    engine = _engine(_model("a/model-1", provider="a"), adapters=sim)
    # Registry has no vision-capable models at all.
    req = _req(
        messages=[{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}]
    )
    with pytest.raises(NoCandidatesError):
        await engine.complete(req)


# --------------------------------------------------------------------------
# 2. Failure-driven routing (§48 contracts)
# --------------------------------------------------------------------------

async def test_rate_limit_falls_back_to_next_candidate(sim):
    sim["a"].script("model-1", "rate_limit")
    engine = _engine(
        _model("a/model-1", provider="a", priority=0),
        _model("b/model-2", provider="b", priority=1),
        adapters=sim,
        max_retries=0,  # no retries: straight to fallback
    )
    resp = await engine.complete(_req())
    assert resp.model == "b/model-2"
    assert resp.from_fallback is True
    assert resp.attempts == 2
    assert [c[0] for c in sim["a"].calls] == ["model-1"]
    assert [c[0] for c in sim["b"].calls] == ["model-2"]


async def test_server_error_retries_then_falls_back(sim):
    sim["a"].script("model-1", "server_error")
    engine = _engine(
        _model("a/model-1", provider="a", priority=0),
        _model("b/model-2", provider="b", priority=1),
        adapters=sim,
        max_retries=2,  # a gets 3 tries (retryable), then b
    )
    resp = await engine.complete(_req())
    assert resp.model == "b/model-2"
    assert len(sim["a"].calls) == 3          # 1 attempt + 2 retries
    assert len(sim["b"].calls) == 1


async def test_auth_error_not_retried_moves_on(sim):
    sim["a"].script("model-1", "auth_error")
    engine = _engine(
        _model("a/model-1", provider="a", priority=0),
        _model("b/model-2", provider="b", priority=1),
        adapters=sim,
        max_retries=3,  # auth is NOT retryable — 3 retries must NOT happen
    )
    resp = await engine.complete(_req())
    assert resp.model == "b/model-2"
    assert len(sim["a"].calls) == 1          # single attempt, zero retries


async def test_model_unavailable_moves_on(sim):
    sim["a"].script("model-1", "model_unavailable")
    engine = _engine(
        _model("a/model-1", provider="a", priority=0),
        _model("b/model-2", provider="b", priority=1),
        adapters=sim,
    )
    resp = await engine.complete(_req())
    assert resp.model == "b/model-2"


async def test_context_length_skips_to_larger_context(sim):
    sim["a"].script("model-1", "context_length")
    engine = _engine(
        _model("a/model-1", provider="a", priority=0, context=8_000),
        _model("b/model-2", provider="b", priority=1, context=128_000),
        adapters=sim,
    )
    resp = await engine.complete(_req())
    assert resp.model == "b/model-2"


async def test_all_providers_fail_returns_normalized_error(sim):
    sim["a"].script("model-1", "server_error")
    sim["b"].script("model-2", "rate_limit")
    engine = _engine(
        _model("a/model-1", provider="a", priority=0),
        _model("b/model-2", provider="b", priority=1),
        adapters=sim,
        max_retries=0,
    )
    with pytest.raises(AllProvidersFailedError) as excinfo:
        await engine.complete(_req())
    err = excinfo.value
    assert err.category == "all_providers_failed_error"
    assert err.status_code == 503
    assert len(err.attempts) == 2
    types = {a["error_type"] for a in err.attempts}
    assert types == {"provider_unavailable_error", "rate_limit_error"}
    assert "all providers failed" in err.safe  # safe message is generic


async def test_invalid_request_error_raised_immediately(sim):
    """A bad REQUEST must not be routed around — it fails everywhere."""
    sim["a"].script("model-1", "invalid_request")
    sim["b"].script("model-2", "invalid_request")
    engine = _engine(
        _model("a/model-1", provider="a", priority=0),
        _model("b/model-2", provider="b", priority=1),
        adapters=sim,
    )
    with pytest.raises(InvalidRequestError):
        await engine.complete(_req())
    assert len(sim["b"].calls) == 0  # never tried: request is bad everywhere


# --------------------------------------------------------------------------
# 3. Quota (spec §10-11)
# --------------------------------------------------------------------------

class _FakeQuotaLedger(QuotaManager):
    """In-memory quota ledger standing in for Postgres."""

    def __init__(self, remaining: dict[tuple[str, str], int]) -> None:
        self.remaining = dict(remaining)
        self.recorded: list[dict] = []

    async def daily_remaining(self, provider: str, model: str, limit: int) -> int | None:
        return self.remaining.get((provider, model))

    async def record_usage(self, **kwargs) -> None:
        self.recorded.append(kwargs)


async def test_quota_exhausted_model_excluded(sim):
    sim["a"].script("model-1", "success")
    quota = _FakeQuotaLedger({("a", "model-1"): 0})  # exhausted today
    engine = _engine(
        _model("a/model-1", provider="a", priority=0, daily_limit=50),
        _model("b/model-2", provider="b", priority=1),
        adapters=sim,
        quota=quota,
    )
    resp = await engine.complete(_req())
    assert resp.model == "b/model-2"   # a excluded despite higher priority
    assert len(sim["a"].calls) == 0    # never called


async def test_quota_remaining_allows_selection(sim):
    quota = _FakeQuotaLedger({("a", "model-1"): 12})
    engine = _engine(
        _model("a/model-1", provider="a", priority=0, daily_limit=50),
        adapters=sim,
        quota=quota,
    )
    resp = await engine.complete(_req())
    assert resp.model == "a/model-1"


async def test_usage_recorded_for_success(sim):
    quota = _FakeQuotaLedger({})
    engine = _engine(
        _model("a/model-1", provider="a", priority=0,
               price=(1.0, 2.0)),
        adapters=sim,
        quota=quota,
    )
    await engine.complete(_req(request_id="req-123"))
    assert len(quota.recorded) == 1
    rec = quota.recorded[0]
    assert rec["request_id"] == "req-123"
    assert rec["provider"] == "a"
    assert rec["outcome"] == "success"
    assert rec["prompt_tokens"] == 5
    assert rec["completion_tokens"] == 9
    assert rec["cost_usd"] == pytest.approx(
        5 / 1_000_000 * 1.0 + 9 / 1_000_000 * 2.0
    )


# --------------------------------------------------------------------------
# 4. Routing policies (spec §9)
# --------------------------------------------------------------------------

async def test_free_only_policy(sim):
    sim["a"].script("paid-model", "success")
    sim["b"].script("free-model", "success")
    engine = _engine(
        _model("a/paid-model", provider="a", priority=0, free=False),
        _model("b/free-model", provider="b", priority=1, free=True),
        adapters=sim,
        policy="free_only",
    )
    resp = await engine.complete(_req())
    assert resp.model == "b/free-model"
    assert len(sim["a"].calls) == 0


async def test_cost_policy_picks_free_before_paid(sim):
    engine = _engine(
        _model("a/paid-model", provider="a", priority=0, free=False,
               price=(3.0, 15.0)),
        _model("b/free-model", provider="b", priority=1, free=True,
               price=(0.0, 0.0)),
        adapters=sim,
        policy="cost",
    )
    resp = await engine.complete(_req())
    assert resp.model == "b/free-model"


async def test_cost_policy_picks_cheapest_paid(sim):
    engine = _engine(
        _model("a/expensive", provider="a", priority=0,
               price=(5.0, 15.0)),
        _model("b/cheap", provider="b", priority=1,
               price=(0.1, 0.3)),
        adapters=sim,
        policy="cost",
    )
    resp = await engine.complete(_req())
    assert resp.model == "b/cheap"


async def test_local_first_policy(sim):
    ollama = SimulatedProvider("ollama")
    engine = _engine(
        _model("a/paid-model", provider="a", priority=0, free=False),
        _model("ollama/local-model", provider="ollama", priority=1, free=True),
        adapters={**sim, "ollama": ollama},
        policy="local_first",
    )
    resp = await engine.complete(_req())
    assert resp.model == "ollama/local-model"


async def test_max_cost_excludes_expensive_models(sim):
    engine = _engine(
        _model("a/expensive", provider="a", priority=0,
               price=(5.0, 15.0)),   # 20 USD per 1M tokens combined
        _model("b/cheap", provider="b", priority=1,
               price=(0.1, 0.3)),    # 0.4 USD per 1M tokens combined
        adapters=sim,
    )
    resp = await engine.complete(_req(max_cost_usd=10.0))  # 20 > 10, 0.4 <= 10
    assert resp.model == "b/cheap"
    assert len(sim["a"].calls) == 0


async def test_latency_policy_uses_recent_stats(sim):
    engine = _engine(
        _model("a/model-1", provider="a", priority=0),
        _model("b/model-2", provider="b", priority=1),
        adapters=sim,
        policy="latency",
    )
    # Seed stats: b is fast, a is slow.
    engine.circuits.record_success("b/model-2", 50)
    engine.circuits.record_success("a/model-1", 5000)
    resp = await engine.complete(_req())
    assert resp.model == "b/model-2"


async def test_capability_filter_requires_tool_support(sim):
    no_tools = _model("a/no-tools", provider="a", priority=0,
                      capabilities=frozenset({"streaming", "coding"}))
    engine = _engine(
        no_tools,
        _model("b/with-tools", provider="b", priority=1),
        adapters=sim,
    )
    resp = await engine.complete(
        _req(tools=[{"type": "function", "function": {"name": "f", "parameters": {}}}])
    )
    assert resp.model == "b/with-tools"
    assert len(sim["a"].calls) == 0


async def test_min_context_filter(sim):
    engine = _engine(
        _model("a/small", provider="a", priority=0, context=8_000),
        _model("b/big", provider="b", priority=1, context=131_072),
        adapters=sim,
    )
    resp = await engine.complete(_req(min_context=32_000))
    assert resp.model == "b/big"


# --------------------------------------------------------------------------
# 5. Tool calling (spec §18-19)
# --------------------------------------------------------------------------

async def test_tool_call_preserved_canonically(sim):
    sim["a"].script("model-1", "tool_call")
    engine = _engine(_model("a/model-1", provider="a"), adapters=sim)
    resp = await engine.complete(_req(tools=[{"type": "function", "function": {"name": "run_shell"}}]))
    assert resp.finish_reason == "tool_calls"
    assert resp.tool_calls is not None
    tc = resp.tool_calls[0]
    assert tc["id"] == "call_a"
    assert tc["name"] == "run_shell"
    assert tc["arguments"] == '{"cmd": "echo hi"}'
    assert resp.content == ""


# --------------------------------------------------------------------------
# 6. Streaming (spec §17)
# --------------------------------------------------------------------------

async def _collect_stream(engine, req):
    decision = await engine.plan(req)
    return [ev async for ev in engine.stream(req, decision)]


async def test_streaming_normalized_events(sim):
    engine = _engine(_model("a/model-1", provider="a"), adapters=sim)
    events = await _collect_stream(engine, _req(stream=True))
    types = [ev["type"] for ev in events]
    assert types == ["text_delta", "text_delta", "finish", "usage"]
    assert events[0]["text"] == "hello "
    assert events[1]["text"] == "from a"
    assert events[2]["finish_reason"] == "stop"
    assert events[3]["prompt_tokens"] == 5


async def test_streaming_tool_call_deltas(sim):
    sim["a"].script("model-1", "stream_tool_call")
    engine = _engine(_model("a/model-1", provider="a"), adapters=sim)
    events = await _collect_stream(engine, _req(stream=True))
    types = [ev["type"] for ev in events]
    assert types == ["tool_call_delta", "tool_call_delta", "finish", "usage"]
    assert events[2]["finish_reason"] == "tool_calls"
    assert events[2]["tool_calls"][0]["arguments"] == '{"cmd": "echo hi"}'


async def test_stream_falls_back_before_first_delta(sim):
    sim["a"].script("model-1", "server_error")
    engine = _engine(
        _model("a/model-1", provider="a", priority=0),
        _model("b/model-2", provider="b", priority=1),
        adapters=sim,
        max_retries=0,
    )
    events = await _collect_stream(engine, _req(stream=True))
    assert events[0]["text"] == "hello "          # from b
    assert events[-1]["type"] == "usage"
    assert [c[0] for c in sim["b"].calls] == ["model-2"]


async def test_stream_mid_flight_failure_emits_error_event(sim):
    class _BurstThenFail(SimulatedProvider):
        async def stream(self, req, model_id):
            yield {"type": "text_delta", "text": "partial "}
            raise sim["a"]._raise_for("server_error", model_id)

    sim["a"] = _BurstThenFail("a")
    sim["a"].calls = []
    engine = _engine(
        _model("a/model-1", provider="a", priority=0),
        _model("b/model-2", provider="b", priority=1),
        adapters=sim,
        max_retries=0,
    )
    events = await _collect_stream(engine, _req(stream=True))
    # Content was already forwarded: NO switching to b, error event emitted.
    assert [ev["type"] for ev in events] == ["text_delta", "error"]
    assert len(sim["b"].calls) == 0


# --------------------------------------------------------------------------
# 7. Circuit breaker (spec §15)
# --------------------------------------------------------------------------

async def test_circuit_breaker_opens_after_threshold(sim):
    """Three consecutive failures on a open its circuit; after that, a is
    excluded from the candidate list and b serves everything."""
    sim["a"].script("model-1", "server_error")
    circuits = CircuitRegistry(threshold=3, open_for_s=60)
    engine = _engine(
        _model("a/model-1", provider="a", priority=0),
        _model("b/model-2", provider="b", priority=1),
        adapters=sim,
        circuits=circuits,
        max_retries=0,
    )
    for _ in range(3):
        resp = await engine.complete(_req())
        assert resp.model == "b/model-2"
    assert circuits.allow("a/model-1") is False
    # Next request: a is not even tried anymore.
    resp = await engine.complete(_req())
    assert resp.model == "b/model-2"
    assert len(sim["a"].calls) == 3   # no new calls to a after opening


async def test_circuit_recovers_after_open_period(sim):
    sim["a"].script("model-1", "server_error")
    sim["b"].script("model-2", "server_error")
    circuits = CircuitRegistry(threshold=2, open_for_s=0.05)
    engine = _engine(
        _model("a/model-1", provider="a", priority=0),
        _model("b/model-2", provider="b", priority=1),
        adapters=sim,
        circuits=circuits,
        max_retries=0,
    )
    for _ in range(2):
        with pytest.raises(AllProvidersFailedError):
            await engine.complete(_req())
    assert circuits.allow("a/model-1") is False
    await asyncio.sleep(0.06)   # open period elapsed -> half-open probe
    assert circuits.allow("a/model-1") is True


# --------------------------------------------------------------------------
# 8. Deadline (spec §16, §28)
# --------------------------------------------------------------------------

async def test_deadline_bounds_total_work(sim):
    """A tiny deadline must stop the engine before it burns every candidate:
    three slow candidates x three retries would take ~1s+ of sleep each
    without deadline bounding; the engine must stop at the deadline."""
    import time

    class _SlowTimeoutProvider(SimulatedProvider):
        def __init__(self, name: str, delay_s: float) -> None:
            super().__init__(name)
            self.delay_s = delay_s

        async def complete(self, req, model_id):
            self.calls.append((model_id, req))
            await asyncio.sleep(self.delay_s)
            raise self._raise_for("timeout", model_id)

    slow = {
        "a": _SlowTimeoutProvider("a", 0.1),
        "b": _SlowTimeoutProvider("b", 0.1),
        "c": _SlowTimeoutProvider("c", 0.1),
    }
    engine = _engine(
        _model("a/model-1", provider="a", priority=0),
        _model("b/model-2", provider="b", priority=1),
        _model("c/model-3", provider="c", priority=2),
        adapters=slow,
        deadline_s=0.15,
        max_retries=2,
    )
    started = time.monotonic()
    with pytest.raises(AllProvidersFailedError) as excinfo:
        await engine.complete(_req())
    elapsed = time.monotonic() - started
    assert elapsed < 0.6, f"deadline ignored: took {elapsed:.2f}s"
    # The attempt log records where the deadline cut the work short.
    error_types = {a["error_type"] for a in excinfo.value.attempts}
    assert "deadline_exceeded" in error_types
    # Candidate c was never reached once the deadline ran out.
    assert len(slow["c"].calls) == 0
