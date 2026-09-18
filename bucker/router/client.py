"""Model router — the one place the system talks to an LLM.

[HAND] — this file is the mechanism behind the project's central claim: *the
LLM is the replaceable part*. Two rules keep that true, and both are enforced
rather than trusted:

  1. The model name comes from config/env. Never a literal in this file or any
     other (CI greps for it).
  2. Every request and response is stored verbatim, content-addressed, before
     anything downstream sees it. That archive is what makes replay possible.

Two modes:

  live      — call the provider, store the response, write a recording.
  recorded  — answer from the stored recording. No network, no cost, no
              nondeterminism. This is the default, so tests and day-to-day
              iteration are free.

Determinism here is *record-and-replay*, not a claim that LLMs are
deterministic. The system never re-invokes a model to reproduce a past run; it
replays what that model actually said, byte for byte.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bucker.config import settings
from bucker.core.blob import BlobStore
from bucker.gateway.errors import GatewayError
from bucker.gateway.models import InferenceRequest
from bucker.gateway.quota import QuotaManager
from bucker.gateway.routing import RouterEngine


class RecordingMissing(Exception):
    """Recorded mode was asked for a call it has never seen.

    Deliberately loud. The alternative — silently falling back to a live call —
    would make a "free, deterministic" test suite quietly cost money and vary
    between runs, which is precisely the failure this architecture exists to
    prevent.
    """


class ModelCallFailed(Exception):
    """The provider call failed after the router's own retries."""


@dataclass(frozen=True, slots=True)
class ModelResponse:
    text: str
    model: str
    cost_usd: float | None   # None = unknown (pricing metadata missing)
    latency_ms: int
    raw_ref: str                  # blob ref: the verbatim provider response
    request_ref: str              # blob ref: the verbatim request
    from_recording: bool
    usage: dict[str, Any] = field(default_factory=dict)
    cost_unknown: bool = False
    #: Canonical tool calls [{id, name, arguments(json str)}] — the
    #: gateway preserves them across the ModelRouter boundary so a
    #: future tool-calling worker (Phase 4) can execute them. Recorded
    #: mode replays them byte-for-byte like text.
    tool_calls: list[dict] | None = None
    finish_reason: str = "stop"
    #: Time to first token (streaming) or None for non-stream/replay.
    #: Monotonic ms from request start to first forwarded delta.
    ttft_ms: int | None = None
    #: Provider-reported prompt-cache hits (None = not reported).
    cached_tokens: int | None = None


def _redact_messages(messages: list[dict]) -> list[dict]:
    """Deep-redact credential-shaped spans in archived prompt messages."""
    from bucker.security.secrets import redact

    out = []
    for msg in messages:
        if isinstance(msg, dict) and isinstance(msg.get("content"), str):
            out.append({**msg, "content": redact(msg["content"])[0]})
        else:
            out.append(msg)
    return out


def _redact_raw(raw: dict) -> dict:
    """Redact credential-shaped spans in an archived raw provider response."""
    from bucker.security.secrets import redact

    out = dict(raw)
    try:
        for choice in out.get("choices", []) or []:
            message = choice.get("message") or {}
            if isinstance(message.get("content"), str):
                message["content"] = redact(message["content"])[0]
            if isinstance(message.get("reasoning_content"), str):
                message["reasoning_content"] = redact(message["reasoning_content"])[0]
    except Exception:  # noqa: BLE001 — redaction must never break storage
        pass
    return out


def request_digest(
    model: str,
    messages: list[dict],
    temperature: float,
    max_tokens: int | None = None,
) -> str:
    """Stable fingerprint of a request. Same inputs -> same recording.

    Sorted keys and a fixed separator so dict ordering cannot change the hash;
    otherwise the same logical call could miss its own recording.

    ``max_tokens`` is part of the identity because it can truncate the response:
    the same prompt at 500 tokens and at 8000 tokens are genuinely different
    calls, and replaying one as the other would be a lie about what happened.
    """
    payload = json.dumps(
        {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class RecordingStore:
    """Maps a request digest to the recorded response metadata."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, digest: str) -> Path:
        return self.root / f"{digest}.json"

    def has(self, digest: str) -> bool:
        return self._path(digest).exists()

    def get(self, digest: str) -> dict[str, Any]:
        path = self._path(digest)
        if not path.exists():
            raise RecordingMissing(digest)
        return json.loads(path.read_text(encoding="utf-8"))

    def put(self, digest: str, record: dict[str, Any]) -> None:
        # Write-then-rename: a crash mid-write must not leave a half-written
        # recording that a later run would treat as valid.
        path = self._path(digest)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
        tmp.replace(path)

    def count(self) -> int:
        return len(list(self.root.glob("*.json")))


class ModelRouter:
    """Every LLM call in the system goes through here. No exceptions."""

    def __init__(
        self,
        blobs: BlobStore | None = None,
        *,
        model: str | None = None,
        mode: str | None = None,
        recordings: RecordingStore | None = None,
        fallbacks: tuple[str, ...] | None = None,
        engine: RouterEngine | None = None,
    ) -> None:
        # Defaults resolve from config, never from literals in this file.
        self.model = model or settings.model
        self.model_fallbacks = (
            tuple(fallbacks) if fallbacks is not None else settings.model_fallbacks
        )
        self.mode = mode or settings.model_mode
        self.blobs = blobs or BlobStore(settings.blob_root)
        self.recordings = recordings or RecordingStore(
            Path(settings.blob_root).parent / "recordings"
        )
        # The inference gateway engine behind LIVE mode (Phase 1 of the
        # ModelRouter-v2 bridge): capability filtering, policy routing,
        # circuit breakers, and fallback now live in bucker/gateway/, not
        # in a hardcoded chain here. RECORDED mode never touches the engine
        # — replay is deterministic by construction. Injected for tests;
        # the default is the production engine. The internal path does not
        # write the gateway_usage ledger (that covers /v1 calls); its audit
        # is the recording envelope (request/routing/response).
        self.engine = engine if engine is not None else RouterEngine(
            quota=QuotaManager(),
        )
        # The router's configured chain (model + fallbacks) is the
        # operator's choice; make it routable in the engine even when the
        # ids are not in the curated catalog (e.g. adaptive-selected ones).
        # Existing entries keep their registry priorities — the REQUESTED
        # model always goes first via the engine's explicit preference.
        for i, cid in enumerate([self.model, *self.model_fallbacks]):
            if cid:
                self.engine.registry.ensure(cid, priority=i)

        if self.mode not in ("live", "recorded"):
            raise ValueError(
                f"BUCKER_MODEL_MODE must be 'live' or 'recorded', got {self.mode!r}"
            )

    def max_tokens_for(self, purpose: str) -> int:
        """Output ceiling for this component. Never None — see config.py."""
        return {
            "planner": settings.max_tokens_planner,
            "worker": settings.max_tokens_worker,
            "critic": settings.max_tokens_critic,
        }.get(purpose, settings.max_tokens_default)

    # ---------------------------------------------------------------------
    async def complete(
        self,
        messages: list[dict],
        *,
        purpose: str,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
    ) -> ModelResponse:
        """Run one completion.

        ``purpose`` ("planner", "worker", ...) is recorded for telemetry so cost
        can later be attributed per component, not just per task, and it selects
        the default output ceiling.

        ``tools`` / ``tool_choice`` pass through to the gateway engine — the
        Phase 4 tool-calling worker surface. Recorded mode replays stored
        tool-call responses like any other response.
        """
        if max_tokens is None:
            max_tokens = self.max_tokens_for(purpose)

        digest = request_digest(self.model, messages, temperature, max_tokens)
        # Hardening review: prompts are archived for replay — redact
        # credential-shaped spans so secrets/proprietary code the user
        # pasted do not live verbatim in the blobstore.
        request_ref = self.blobs.put_json(
            {
                "model": self.model,
                "messages": _redact_messages(messages),
                "temperature": temperature,
                "max_tokens": max_tokens,
                "purpose": purpose,
            }
        )

        if self.mode == "recorded":
            return self._from_recording(digest, request_ref)

        return await self._live(
            digest, request_ref, messages, purpose, temperature, max_tokens,
            tools=tools, tool_choice=tool_choice,
        )

    async def stream_complete(
        self,
        messages: list[dict],
        *,
        purpose: str,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
    ):
        """Streaming completion: yield text deltas as they arrive.

        Time-to-first-visible-token drops even when total generation time
        does not — the caller can render the first delta immediately
        instead of waiting for the full completion.

        Yields ``{"type": "text_delta", "text": ...}`` chunks, then a final
        ``{"type": "final", "response": ModelResponse}``. The final response
        carries the same recording envelope as :meth:`complete` (same
        request digest), so replay stays byte-identical whether the
        original call streamed or not. Recorded mode replays the stored
        text in one chunk — deterministic, no network.

        Mid-stream failure yields ``{"type": "error", ...}`` with the
        engine's honest fields (``interrupted``, ``forwarded``,
        ``discard_partial``) and NO final — mirroring the gateway rule that
        fallback only happens before the first forwarded delta. A caller
        that already rendered deltas must discard the partial output and
        retry the whole turn on another model; resuming a half-generated
        response elsewhere is incoherent. Nothing is recorded on this path,
        so the retry replays cleanly.
        """
        if max_tokens is None:
            max_tokens = self.max_tokens_for(purpose)

        digest = request_digest(self.model, messages, temperature, max_tokens)
        request_ref = self.blobs.put_json(
            {
                "model": self.model,
                "messages": _redact_messages(messages),
                "temperature": temperature,
                "max_tokens": max_tokens,
                "purpose": purpose,
                "stream": True,
            }
        )

        if self.mode == "recorded":
            resp = self._from_recording(digest, request_ref)
            if resp.text:
                yield {"type": "text_delta", "text": resp.text}
            yield {"type": "final", "response": resp}
            return

        # Live: forward deltas immediately; accumulate for the recording.
        import time as _time

        request = InferenceRequest(
            purpose=purpose,
            messages=messages,
            model=self.model,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            tool_choice=tool_choice,
            stream=True,
        )
        started = _time.monotonic()
        try:
            decision = await self.engine.plan(request)
        except GatewayError as exc:
            raise ModelCallFailed(f"all models in the chain failed: {exc}") from exc

        chunks: list[str] = []
        tool_calls: list[dict] | None = None
        finish_reason = "stop"
        ttft_ms: int | None = None
        async for ev in self.engine.stream(request, decision):
            kind = ev.get("type")
            if kind == "text_delta":
                if ttft_ms is None:
                    ttft_ms = int((_time.monotonic() - started) * 1000)
                chunks.append(ev.get("text", ""))
                yield {"type": "text_delta", "text": ev.get("text", "")}
            elif kind == "tool_call_delta":
                if ttft_ms is None:
                    ttft_ms = int((_time.monotonic() - started) * 1000)
                yield ev
            elif kind == "finish":
                tool_calls = ev.get("tool_calls")
                finish_reason = ev.get("finish_reason") or "stop"
            elif kind == "usage":
                # Final event: build + archive the same envelope as _live
                # so a streamed call replays identically via complete().
                text = "".join(chunks)
                latency_ms = int((_time.monotonic() - started) * 1000)
                selected = decision.selected or self.model
                usage = {
                    "prompt_tokens": ev.get("prompt_tokens", 0),
                    "completion_tokens": ev.get("completion_tokens", 0),
                    "total_tokens": ev.get("prompt_tokens", 0)
                    + ev.get("completion_tokens", 0),
                    "cost_usd": ev.get("cost_usd"),
                    "cached_tokens": ev.get("cached_tokens"),
                    "ttft_ms": ev.get("ttft_ms", ttft_ms),
                }
                raw_ref = self.blobs.put_json(
                    _redact_raw({"text": text, "streamed": True})
                )
                cost_usd = usage.get("cost_usd")
                cost_unknown = cost_usd is None
                try:
                    served_model = selected
                    self.recordings.put(
                        digest,
                        {
                            "model": self.model,
                            "model_served": served_model,
                            "purpose": purpose,
                            "text": text,
                            "raw_ref": raw_ref,
                            "request_ref": request_ref,
                            "cost_usd": cost_usd,
                            "cost_unknown": cost_unknown,
                            "latency_ms": latency_ms,
                            "ttft_ms": ttft_ms,
                            "usage": usage,
                            "finish_reason": finish_reason,
                            "tool_calls": tool_calls,
                            "routing": {
                                "policy": decision.policy,
                                "config_version": self.engine.registry.config_version(),
                                "candidates": list(decision.candidates),
                                "selected": {"model": served_model},
                                "reason": "stream",
                                "fallback_attempts": list(decision.attempts),
                            },
                        },
                    )
                except Exception:
                    pass
                resp = ModelResponse(
                    text=text,
                    model=served_model,
                    cost_usd=cost_usd,
                    cost_unknown=cost_unknown,
                    latency_ms=latency_ms,
                    raw_ref=raw_ref,
                    request_ref=request_ref,
                    from_recording=False,
                    usage=usage,
                    tool_calls=tool_calls,
                    finish_reason=finish_reason,
                    ttft_ms=ttft_ms,
                    cached_tokens=usage.get("cached_tokens"),
                )
                yield {"type": "final", "response": resp}
                return
            elif kind == "error":
                # Honest interruption: forward the engine's fields so the
                # caller knows partial output (if any) must be discarded.
                # No recording is written — the retried whole turn records
                # under its own digest when it succeeds.
                forwarded = bool(chunks) or ev.get("forwarded", False)
                yield {
                    "type": "error",
                    "error_type": ev.get("error_type"),
                    "message": ev.get("message")
                    or ev.get("honest_message")
                    or "stream interrupted",
                    "interrupted": ev.get("interrupted", True),
                    "forwarded": forwarded,
                    "discard_partial": True,
                    "ttft_ms": ttft_ms,
                }
                return

    # ------------------------------------------------------------ replay --
    def _from_recording(self, digest: str, request_ref: str) -> ModelResponse:
        if not self.recordings.has(digest):
            raise RecordingMissing(
                f"No recording for request {digest[:12]}... (model={self.model}).\n"
                f"Run once with BUCKER_MODEL_MODE=live to record it, or check "
                f"whether the prompt changed — any edit to the prompt changes "
                f"the digest and needs a fresh recording."
            )

        record = self.recordings.get(digest)

        # Verify the archived response still hashes to its ref. A tampered or
        # corrupted blob must surface as an error, never be replayed as truth.
        raw_ref = record["raw_ref"]
        if not self.blobs.verify(raw_ref):
            raise RecordingMissing(
                f"Recording {digest[:12]}... points at blob {raw_ref} which is "
                f"missing or corrupted. The archive cannot be trusted."
            )

        return ModelResponse(
            text=record["text"],
            model=record["model"],
            cost_usd=0.0,             # replay is free; the original cost is in the record
            latency_ms=0,
            raw_ref=raw_ref,
            request_ref=request_ref,
            from_recording=True,
            usage=record.get("usage", {}),
            cost_unknown=bool(record.get("cost_unknown", False)),
            tool_calls=record.get("tool_calls"),
            finish_reason=record.get("finish_reason", "stop"),
            ttft_ms=record.get("ttft_ms"),
            cached_tokens=(record.get("usage") or {}).get("cached_tokens"),
        )

    # -------------------------------------------------------------- live --
    async def _live(
        self,
        digest: str,
        request_ref: str,
        messages: list[dict],
        purpose: str,
        temperature: float,
        max_tokens: int | None,
        *,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
    ) -> ModelResponse:
        """Live inference through the gateway engine (ModelRouter-v2).

        The engine owns provider selection, fallback, retries, circuits,
        and capability filtering. The recording captures the full envelope
        — request, ROUTING DECISION, response — so replay is a pure lookup
        and never re-decides routing (live = intelligent routing, replay =
        historical reconstruction; the two must not mix).
        """
        request = InferenceRequest(
            purpose=purpose,
            messages=messages,
            model=self.model,          # the REQUESTED model (a preference)
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            tool_choice=tool_choice,
        )
        try:
            response, decision = await self.engine.complete_with_decision(request)
        except GatewayError as exc:
            # Preserve the router's public error contract for callers.
            raise ModelCallFailed(
                f"all models in the chain failed: {exc}"
            ) from exc

        # Redact credential-shaped spans from the archived raw provider
        # response (content can carry secrets the prompt-injection surface
        # cares about).
        raw_ref = self.blobs.put_json(
            _redact_raw(response.raw or {"text": response.content})
        )

        cost_usd = response.usage.get("cost_usd")
        # Hardening review: never record a fabricated $0.00 for a real
        # call. Missing pricing metadata makes the cost UNKNOWN —
        # telemetry stores NULL and budgeted workflows fail closed.
        cost_unknown = cost_usd is None

        self.recordings.put(
            digest,
            {
                "model": self.model,          # the configured primary (replay key)
                "model_served": response.model,  # the one that actually answered
                "purpose": purpose,
                "text": response.content,
                "raw_ref": raw_ref,
                "request_ref": request_ref,
                "cost_usd": cost_usd,
                "cost_unknown": cost_unknown,
                "latency_ms": response.latency_ms,
                "ttft_ms": response.ttft_ms,
                "usage": response.usage,
                "finish_reason": response.finish_reason,
                "tool_calls": response.tool_calls,
                # The routing envelope: what was requested, what was
                # decided, and why. This is what replay DOES NOT re-derive.
                "routing": {
                    "policy": decision.policy,
                    "config_version": self.engine.registry.config_version(),
                    "candidates": [
                        {"provider": m.provider, "model": m.canonical_id}
                        for cid in decision.candidates
                        for m in [self.engine.registry.get(cid)]
                        if m is not None
                    ],
                    "selected": {
                        "provider": response.provider,
                        "model": response.model,
                    },
                    "reason": (
                        "primary_candidate"
                        if not decision.attempts
                        else "fallback_after_failure"
                    ),
                    "fallback_attempts": [
                        {
                            "provider": a["provider"],
                            "model": a["model"],
                            "error": a["error_type"],
                        }
                        for a in decision.attempts
                    ],
                },
            },
        )

        return ModelResponse(
            text=response.content,
            model=response.model,
            cost_usd=cost_usd,
            cost_unknown=cost_unknown,
            latency_ms=response.latency_ms,
            raw_ref=raw_ref,
            request_ref=request_ref,
            from_recording=False,
            usage=response.usage,
            tool_calls=response.tool_calls,
            finish_reason=response.finish_reason,
            ttft_ms=response.ttft_ms,
            cached_tokens=response.cached_tokens,
        )
