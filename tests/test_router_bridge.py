"""ModelRouter-v2 bridge tests — the definition-of-done contracts.

Live  = intelligent routing:  ModelRouter -> RouterEngine -> provider
Replay = historical reconstruction: ModelRouter -> recording -> stored response

Replay NEVER re-decides routing — no engine call, no quota check, no
circuit check, no network. That is the invariant this file exists to
prove (Phase 2 of the ModelRouter-v2 bridge).

All providers are SimulatedProviders: hermetic, no network, no money.
"""

from __future__ import annotations

import json

from bucker.core.blob import BlobStore
from bucker.gateway.adapters import RawCompletion, SimulatedProvider
from bucker.gateway.models import InferenceRequest
from bucker.gateway.quota import QuotaManager
from bucker.gateway.registry import GatewayModel, ModelRegistry
from bucker.gateway.routing import RouterEngine
from bucker.router.client import ModelRouter, RecordingStore, request_digest

# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------

def _model(
    canonical_id: str,
    *,
    provider: str | None = None,
    priority: int = 100,
    price: tuple[float, float] | None = None,
    capabilities: frozenset[str] | None = None,
) -> GatewayModel:
    p = provider or canonical_id.split("/")[0]
    rest = canonical_id.split("/", 1)[1]
    return GatewayModel(
        canonical_id=canonical_id,
        provider=p,
        provider_model_id=rest,
        family=rest,
        context=128_000,
        max_output=8192,
        capabilities=capabilities or frozenset({"tools", "streaming", "coding"}),
        price_input_per_m=price[0] if price else None,
        price_output_per_m=price[1] if price else None,
        free=False,
        priority=priority,
    )


def _engine(
    models: list[GatewayModel],
    adapters: dict,
    *,
    policy: str = "priority",
    max_retries: int = 0,
) -> RouterEngine:
    return RouterEngine(
        registry=ModelRegistry({m.canonical_id: m for m in models}),
        adapters=adapters,
        quota=QuotaManager(),  # no pool -> quota checks are no-ops
        policy=policy,
        max_retries=max_retries,
        deadline_s=30.0,
        timeout_s=5.0,
    )


def _router(
    tmp_path,
    engine: RouterEngine,
    *,
    model: str = "a/model-1",
    mode: str = "live",
) -> ModelRouter:
    return ModelRouter(
        BlobStore(tmp_path / "blobs"),
        model=model,
        mode=mode,
        recordings=RecordingStore(tmp_path / "recordings"),
        engine=engine,
    )


def _sim_pair() -> tuple[dict, SimulatedProvider, SimulatedProvider]:
    a = SimulatedProvider("a")
    b = SimulatedProvider("b")
    return {"a": a, "b": b}, a, b


_MSGS = [{"role": "user", "content": "do the thing"}]


# --------------------------------------------------------------------------
# DoD 1 — internal live request goes through the engine
# --------------------------------------------------------------------------

async def test_live_internal_request_through_engine(tmp_path):
    adapters, a, _ = _sim_pair()
    engine = _engine(
        [_model("a/model-1", provider="a", priority=0)],
        adapters,
    )
    router = _router(tmp_path, engine)
    resp = await router.complete(_MSGS, purpose="worker")
    assert resp.from_recording is False
    assert resp.text == "hello from a/model-1"
    assert resp.model == "a/model-1"
    assert [c[0] for c in a.calls] == ["model-1"]
    # The recording was written for future replay.
    digest = request_digest(router.model, _MSGS, 0.0, router.max_tokens_for("worker"))
    assert router.recordings.has(digest)


# --------------------------------------------------------------------------
# DoD 2 — provider fallback with the routing envelope
# --------------------------------------------------------------------------

async def test_fallback_records_routing_envelope(tmp_path):
    adapters, a, b = _sim_pair()
    a.script("model-1", "rate_limit")
    engine = _engine(
        [
            _model("a/model-1", provider="a", priority=0),
            _model("b/model-2", provider="b", priority=1),
        ],
        adapters,
    )
    router = _router(tmp_path, engine)
    resp = await router.complete(_MSGS, purpose="worker")
    assert resp.model == "b/model-2"
    assert resp.from_recording is False

    digest = request_digest(router.model, _MSGS, 0.0, router.max_tokens_for("worker"))
    record = router.recordings.get(digest)
    routing = record["routing"]
    assert routing["policy"] == "priority"
    assert routing["config_version"]
    assert routing["candidates"] == [
        {"provider": "a", "model": "a/model-1"},
        {"provider": "b", "model": "b/model-2"},
    ]
    assert routing["selected"] == {"provider": "b", "model": "b/model-2"}
    assert routing["reason"] == "fallback_after_failure"
    assert routing["fallback_attempts"] == [
        {"provider": "a", "model": "a/model-1", "error": "rate_limit_error"}
    ]


# --------------------------------------------------------------------------
# DoD 3 — replay determinism: identical response, no engine contact
# --------------------------------------------------------------------------

async def test_replay_is_deterministic_and_never_re_decides_routing(tmp_path):
    adapters, a, b = _sim_pair()
    a.script("model-1", "rate_limit")
    engine = _engine(
        [
            _model("a/model-1", provider="a", priority=0),
            _model("b/model-2", provider="b", priority=1),
        ],
        adapters,
    )
    router = _router(tmp_path, engine)

    # Live run: A is down, B serves, response X is recorded.
    live = await router.complete(_MSGS, purpose="worker")
    assert live.model == "b/model-2"
    recorded_text = live.text

    # The world flips: A is now healthy, B is down. Replay must NOT notice.
    flipped, a2, b2 = _sim_pair()
    a2.script("model-1", "success")
    b2.script("model-2", "server_error")
    flipped_engine = _engine(
        [
            _model("a/model-1", provider="a", priority=0),
            _model("b/model-2", provider="b", priority=1),
        ],
        flipped,
    )

    class _NeverCalledEngine(RouterEngine):
        """Replay must not touch the engine AT ALL."""

        async def complete_with_decision(self, req, *a, **k):
            raise AssertionError(
                "replay contacted the engine — replay must be a pure lookup"
            )

    spy = _NeverCalledEngine(
        registry=flipped_engine.registry,
        adapters=flipped_engine.adapters,
        quota=flipped_engine.quota,
        policy=flipped_engine.policy,
    )
    replay_router = _router(tmp_path, spy, mode="recorded")

    replayed = await replay_router.complete(_MSGS, purpose="worker")
    assert replayed.from_recording is True
    assert replayed.text == recorded_text          # byte-identical
    assert replayed.model == replay_router.model   # keyed to the REQUESTED model
    assert len(a2.calls) == 0                      # no provider contact
    assert len(b2.calls) == 0


# --------------------------------------------------------------------------
# DoD 4 — adaptive boundary: requirement, not deployment
# --------------------------------------------------------------------------

def test_adaptive_next_model_uses_registry_requirement():
    from bucker.adaptive import next_model
    from bucker.gateway.registry import ModelRegistry

    registry_ids = {m.canonical_id for m in ModelRegistry.default().all()}
    result = next_model("deepseek/deepseek-v4-flash", ["deepseek/deepseek-v4-flash"])
    assert result in registry_ids                    # registry-authoritative
    assert result != "deepseek/deepseek-v4-flash"    # never the model just used


# --------------------------------------------------------------------------
# DoD 5 — tool-call boundary: a returned tool call ends the inference
# --------------------------------------------------------------------------

async def test_tool_call_boundary_no_fallback_no_retry(tmp_path):
    adapters, a, b = _sim_pair()
    a.script("model-1", "tool_call")
    b.script("model-2", "server_error")   # would fail if contacted
    engine = _engine(
        [
            _model("a/model-1", provider="a", priority=0),
            _model("b/model-2", provider="b", priority=1),
        ],
        adapters,
        max_retries=3,                   # retries must NOT happen post-response
    )
    router = _router(tmp_path, engine)
    resp = await router.complete(
        _MSGS,
        purpose="worker",
        tools=[{"type": "function", "function": {"name": "run_shell"}}],
    )
    assert resp.model == "a/model-1"
    assert resp.finish_reason == "tool_calls"
    assert resp.tool_calls is not None
    assert resp.tool_calls[0]["name"] == "run_shell"
    # The gateway is DONE: no fallback, no retry, B never contacted.
    assert len(b.calls) == 0
    assert len(a.calls) == 1


# --------------------------------------------------------------------------
# DoD 6 — multi-turn tool state: the full conversation survives intact
# --------------------------------------------------------------------------

async def test_multiturn_tool_state_preserved_verbatim(tmp_path):
    adapters, a, _ = _sim_pair()
    engine = _engine([_model("a/model-1", provider="a", priority=0)], adapters)
    router = _router(tmp_path, engine)

    conversation = [
        {"role": "user", "content": "read foo.py"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "read_file", "arguments": json.dumps({"path": "foo.py"})},
            }],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "def foo(): pass"},
        {"role": "user", "content": "now fix it"},
    ]
    await router.complete(conversation, purpose="worker")

    sent = a.calls[0][1].messages
    assert sent == conversation  # verbatim — tool calls + tool results intact


# --------------------------------------------------------------------------
# DoD 7 — every recording reconstructs request / routing / response
# --------------------------------------------------------------------------

async def test_recording_envelope_is_complete(tmp_path):
    adapters, _, _ = _sim_pair()
    engine = _engine([_model("a/model-1", provider="a", priority=0)], adapters)
    router = _router(tmp_path, engine)
    await router.complete(_MSGS, purpose="worker")

    digest = request_digest(router.model, _MSGS, 0.0, router.max_tokens_for("worker"))
    record = router.recordings.get(digest)

    # request
    assert record["model"] == "a/model-1"
    assert record["purpose"] == "worker"
    assert record["request_ref"]
    # routing decision
    assert record["routing"]["policy"] == "priority"
    assert record["routing"]["config_version"]
    assert record["routing"]["candidates"]
    assert record["routing"]["selected"]["model"] == "a/model-1"
    assert record["routing"]["reason"] == "primary_candidate"
    assert record["routing"]["fallback_attempts"] == []
    # response
    assert record["text"]
    assert record["usage"]["total_tokens"] >= 1
    assert record["raw_ref"]
    assert record["latency_ms"] >= 0


# --------------------------------------------------------------------------
# Extra regression — the empty-content guard moved into the gateway
# --------------------------------------------------------------------------

class _EmptyContentAdapter(SimulatedProvider):
    """A provider that returns an empty completion (reasoning models can
    burn the whole budget on reasoning_content). The gateway must treat it
    as a failed attempt and fall back — the old router had this guard in
    ModelRouter._live; it now lives in the adapter/engine."""

    async def complete(self, req: InferenceRequest, model_id: str) -> RawCompletion:
        self.calls.append((model_id, req))
        return RawCompletion(
            text="", tool_calls=None, finish_reason="stop",
            usage={"prompt_tokens": 5, "completion_tokens": 100},
        )


async def test_empty_content_falls_back_through_engine(tmp_path):
    a = _EmptyContentAdapter("a")
    b = SimulatedProvider("b")
    engine = _engine(
        [
            _model("a/model-1", provider="a", priority=0),
            _model("b/model-2", provider="b", priority=1),
        ],
        {"a": a, "b": b},
    )
    router = _router(tmp_path, engine)
    resp = await router.complete(_MSGS, purpose="worker")
    assert resp.model == "b/model-2"
    assert resp.text == "hello from b/model-2"


# --------------------------------------------------------------------------
# Extra regression — config version stability (spec §29)
# --------------------------------------------------------------------------

def test_config_version_stable_then_changes():
    m = _model("a/model-1", provider="a", price=(1.0, 2.0))
    reg1 = ModelRegistry({"a/model-1": m})
    assert reg1.config_version() == reg1.config_version()  # stable

    changed = _model("a/model-1", provider="a", price=(9.0, 9.0))
    reg2 = ModelRegistry({"a/model-1": changed})
    assert reg2.config_version() != reg1.config_version()  # pricing drift is visible
