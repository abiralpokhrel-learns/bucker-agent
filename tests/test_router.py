"""Model router tests (steps 14-15).

No network anywhere in this file. Recorded mode is exercised directly and live
mode is never invoked, which is the point: if these tests could reach a
provider, they would cost money and vary between runs.
"""

from __future__ import annotations

import pytest

from bucker.core.blob import BlobStore
from bucker.router.client import (
    ModelCallFailed,
    ModelRouter,
    RecordingMissing,
    RecordingStore,
    request_digest,
)


@pytest.fixture
def router(tmp_path):
    return ModelRouter(
        BlobStore(tmp_path / "blobs"),
        model="test-model-a",
        mode="recorded",
        recordings=RecordingStore(tmp_path / "recordings"),
    )


def record(router: ModelRouter, messages, text, *, temperature=0.0, cost=0.01,
           purpose="planner"):
    """Seed a recording the way a live run would have.

    ``max_tokens`` must match what ``complete()`` will send, since it is part of
    the request identity — a recording made at one ceiling is not a valid replay
    for a call at another.
    """
    digest = request_digest(
        router.model, messages, temperature, router.max_tokens_for(purpose)
    )
    raw_ref = router.blobs.put_json({"choices": [{"message": {"content": text}}]})
    router.recordings.put(digest, {
        "model": router.model,
        "purpose": "test",
        "text": text,
        "raw_ref": raw_ref,
        "request_ref": "sha256:unused",
        "cost_usd": cost,
        "latency_ms": 42,
        "usage": {"total_tokens": 100},
    })
    return digest


# ------------------------------------------------------------- digesting ----
def test_digest_is_stable_across_key_order():
    a = request_digest("m", [{"role": "user", "content": "hi"}], 0.0)
    b = request_digest("m", [{"content": "hi", "role": "user"}], 0.0)
    assert a == b, "dict ordering must not change the recording key"


def test_digest_changes_with_model():
    msgs = [{"role": "user", "content": "hi"}]
    assert request_digest("model-a", msgs, 0.0) != request_digest("model-b", msgs, 0.0)


def test_digest_changes_with_prompt():
    a = request_digest("m", [{"role": "user", "content": "hi"}], 0.0)
    b = request_digest("m", [{"role": "user", "content": "hello"}], 0.0)
    assert a != b


def test_digest_changes_with_temperature():
    msgs = [{"role": "user", "content": "hi"}]
    assert request_digest("m", msgs, 0.0) != request_digest("m", msgs, 0.7)


def test_digest_changes_with_max_tokens():
    """A ceiling can truncate the response, so it is part of the request's
    identity. Replaying an 8000-token recording for a 500-token call would be
    a lie about what happened."""
    msgs = [{"role": "user", "content": "hi"}]
    assert request_digest("m", msgs, 0.0, 500) != request_digest("m", msgs, 0.0, 8000)


# ----------------------------------------------------------- max_tokens ----
def test_max_tokens_is_always_set(router):
    """Never left to the provider.

    Omitting it lets the provider assume the model maximum (64k on current
    frontier models). Providers that reserve credit up front then refuse the
    request outright on a small balance — which is exactly how this was found.
    It is also a cost ceiling, and unbounded generation is unbounded spend.
    """
    for purpose in ("planner", "worker", "anything_else"):
        assert router.max_tokens_for(purpose) > 0


def test_max_tokens_is_sized_per_component(router):
    """A planner emits a small contract; a worker emits a diff."""
    assert router.max_tokens_for("planner") < router.max_tokens_for("worker")


def test_max_tokens_reaches_the_archived_request(router):
    """It must appear in the stored request, or replay cannot reproduce it."""
    import asyncio

    msgs = [{"role": "user", "content": "check the archive"}]
    record(router, msgs, "ok")

    resp = asyncio.run(router.complete(msgs, purpose="planner"))
    archived = router.blobs.get_json(resp.request_ref)
    assert archived["max_tokens"] == router.max_tokens_for("planner")


# -------------------------------------------------------------- recorded ----
async def test_recorded_call_returns_stored_text(router):
    msgs = [{"role": "user", "content": "plan something"}]
    record(router, msgs, '{"ok": true}')

    resp = await router.complete(msgs, purpose="planner")
    assert resp.text == '{"ok": true}'
    assert resp.from_recording is True
    assert resp.model == "test-model-a"


async def test_recorded_calls_are_deterministic(router):
    msgs = [{"role": "user", "content": "same input"}]
    record(router, msgs, "same output")

    first = await router.complete(msgs, purpose="planner")
    second = await router.complete(msgs, purpose="planner")
    assert first.text == second.text
    assert first.raw_ref == second.raw_ref


async def test_replay_is_free(router):
    """Recorded calls must not accrue cost, or budget accounting lies."""
    msgs = [{"role": "user", "content": "x"}]
    record(router, msgs, "y", cost=5.0)

    resp = await router.complete(msgs, purpose="planner")
    assert resp.cost_usd == 0.0


async def test_missing_recording_raises_loudly(router):
    """Must never silently fall back to a live call."""
    with pytest.raises(RecordingMissing, match="No recording"):
        await router.complete(
            [{"role": "user", "content": "never recorded"}], purpose="planner"
        )


async def test_changed_prompt_invalidates_recording(router):
    """Editing a prompt must require a fresh recording, not reuse a stale one."""
    original = [{"role": "user", "content": "version one"}]
    record(router, original, "answer")

    assert (await router.complete(original, purpose="planner")).text == "answer"

    with pytest.raises(RecordingMissing):
        await router.complete(
            [{"role": "user", "content": "version two"}], purpose="planner"
        )


async def test_tampered_blob_is_detected(router):
    """A corrupted archive must fail, not be replayed as truth."""
    msgs = [{"role": "user", "content": "verify me"}]
    digest = record(router, msgs, "original")

    raw_ref = router.recordings.get(digest)["raw_ref"]
    path = router.blobs._path_for(BlobStore._strip(raw_ref))
    path.write_bytes(b'{"choices":[{"message":{"content":"TAMPERED"}}]}')

    with pytest.raises(RecordingMissing, match="corrupted"):
        await router.complete(msgs, purpose="planner")


async def test_request_is_archived_verbatim(router):
    msgs = [{"role": "user", "content": "archive this"}]
    record(router, msgs, "ok")

    resp = await router.complete(msgs, purpose="planner")
    archived = router.blobs.get_json(resp.request_ref)
    assert archived["messages"] == msgs
    assert archived["model"] == "test-model-a"
    assert archived["purpose"] == "planner"


# ---------------------------------------------------------------- config ----
def test_mode_must_be_valid(tmp_path):
    with pytest.raises(ValueError, match="live.*recorded"):
        ModelRouter(BlobStore(tmp_path / "b"), model="m", mode="yolo")


def test_no_hardcoded_model_names_in_source():
    """The 'LLM is a replaceable plugin' claim, enforced in the codebase.

    CI runs the same check; duplicating it here means you find out at test time
    rather than after a push.
    """
    import re
    from pathlib import Path

    pattern = re.compile(r'"(gpt-[\w.\-]+|claude-[\w.\-]+|o[1-9]-[a-z]+)"')
    root = Path(__file__).resolve().parent.parent / "bucker"

    offenders = []
    for path in root.rglob("*.py"):
        if path.name == "config.py":
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(root)}:{i}")

    assert not offenders, f"hardcoded model name: {offenders}"


# --------------------------------------------------------- fallback chain ---
# Live-mode path with litellm stubbed out — no network, no money, but the
# fallback loop itself is exercised. Same spirit as OmniRoute's auto-fallback:
# a dead provider must not take down a task when a working model follows it.


class _FakeResponse:
    """Shape litellm's completion object needs for our consumer."""

    def __init__(self, text: str):
        self._text = text

    def model_dump(self) -> dict:
        return {"choices": [{"message": {"content": self._text}}], "usage": {}}


def _stub_litellm(monkeypatch, acompletion, cost=0.01):
    """Put a fake litellm module where the router's lazy import finds it."""
    import sys
    import types

    fake = types.SimpleNamespace(
        acompletion=acompletion,
        completion_cost=lambda completion_response: cost,
    )
    monkeypatch.setitem(sys.modules, "litellm", fake)


async def test_fallback_serves_when_primary_fails(monkeypatch, tmp_path):
    calls: list[str] = []

    async def acompletion(**kwargs):
        calls.append(kwargs["model"])
        if kwargs["model"] == "primary":
            raise RuntimeError("provider down")
        return _FakeResponse("hello from fallback")

    _stub_litellm(monkeypatch, acompletion)
    router = ModelRouter(
        BlobStore(tmp_path / "blobs"),
        model="primary",
        mode="live",
        recordings=RecordingStore(tmp_path / "recordings"),
        fallbacks=("fallback-a", "fallback-b"),
    )

    resp = await router.complete(
        [{"role": "user", "content": "hi"}], purpose="planner"
    )

    assert calls == ["primary", "fallback-a"]  # only reached the first working one
    assert resp.model == "fallback-a"
    assert resp.text == "hello from fallback"
    assert resp.from_recording is False


async def test_all_fail_raises_with_every_error(monkeypatch, tmp_path):
    async def acompletion(**kwargs):
        raise RuntimeError("nope")

    _stub_litellm(monkeypatch, acompletion)
    router = ModelRouter(
        BlobStore(tmp_path / "blobs"),
        model="primary",
        mode="live",
        recordings=RecordingStore(tmp_path / "recordings"),
        fallbacks=("fallback-a",),
    )

    with pytest.raises(ModelCallFailed, match="all models in the chain failed"):
        await router.complete([{"role": "user", "content": "hi"}], purpose="planner")


async def test_fallback_recording_notes_the_serving_model(monkeypatch, tmp_path):
    """Transparency: the recording keeps the configured primary AND the model
    that actually answered, so telemetry can attribute cost correctly."""
    calls: list[str] = []

    async def acompletion(**kwargs):
        calls.append(kwargs["model"])
        if kwargs["model"] == "primary":
            raise RuntimeError("quota exhausted")
        return _FakeResponse("ok")

    _stub_litellm(monkeypatch, acompletion)
    router = ModelRouter(
        BlobStore(tmp_path / "blobs"),
        model="primary",
        mode="live",
        recordings=RecordingStore(tmp_path / "recordings"),
        fallbacks=("fallback-a",),
    )

    resp = await router.complete([{"role": "user", "content": "hi"}], purpose="planner")

    digest = request_digest(
        "primary",
        [{"role": "user", "content": "hi"}],
        0.0,
        router.max_tokens_for("planner"),
    )
    record = router.recordings.get(digest)
    assert record["model"] == "primary"
    assert record["model_served"] == "fallback-a"
    assert resp.model == "fallback-a"
