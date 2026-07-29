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
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bucker.config import settings
from bucker.core.blob import BlobStore


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
    cost_usd: float
    latency_ms: int
    raw_ref: str                  # blob ref: the verbatim provider response
    request_ref: str              # blob ref: the verbatim request
    from_recording: bool
    usage: dict[str, Any] = field(default_factory=dict)


def request_digest(model: str, messages: list[dict], temperature: float) -> str:
    """Stable fingerprint of a request. Same inputs -> same recording.

    Sorted keys and a fixed separator so dict ordering cannot change the hash;
    otherwise the same logical call could miss its own recording.
    """
    payload = json.dumps(
        {"model": model, "messages": messages, "temperature": temperature},
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
    ) -> None:
        # Defaults resolve from config, never from literals in this file.
        self.model = model or settings.model
        self.mode = mode or settings.model_mode
        self.blobs = blobs or BlobStore(settings.blob_root)
        self.recordings = recordings or RecordingStore(
            Path(settings.blob_root).parent / "recordings"
        )

        if self.mode not in ("live", "recorded"):
            raise ValueError(
                f"BUCKER_MODEL_MODE must be 'live' or 'recorded', got {self.mode!r}"
            )

    # ---------------------------------------------------------------------
    async def complete(
        self,
        messages: list[dict],
        *,
        purpose: str,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> ModelResponse:
        """Run one completion.

        ``purpose`` ("planner", "worker", ...) is recorded for telemetry so cost
        can later be attributed per component, not just per task.
        """
        digest = request_digest(self.model, messages, temperature)
        request_ref = self.blobs.put_json(
            {
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
                "purpose": purpose,
            }
        )

        if self.mode == "recorded":
            return self._from_recording(digest, request_ref)

        return await self._live(
            digest, request_ref, messages, purpose, temperature, max_tokens
        )

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
    ) -> ModelResponse:
        # Imported lazily: litellm is an optional extra, so Phase 0 installs
        # and the whole recorded-mode test suite work without it.
        try:
            import litellm
        except ImportError as exc:  # pragma: no cover
            raise ModelCallFailed(
                "litellm is not installed. Run: uv sync --extra llm"
            ) from exc

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        started = time.perf_counter()
        try:
            response = await litellm.acompletion(**kwargs)
        except Exception as exc:
            raise ModelCallFailed(f"{type(exc).__name__}: {exc}") from exc
        latency_ms = int((time.perf_counter() - started) * 1000)

        raw = response.model_dump() if hasattr(response, "model_dump") else dict(response)
        raw_ref = self.blobs.put_json(raw)

        text = raw["choices"][0]["message"]["content"] or ""
        usage = raw.get("usage") or {}

        try:
            cost_usd = float(litellm.completion_cost(completion_response=response) or 0.0)
        except Exception:
            # Never fail a task because pricing metadata was missing; a wrong
            # cost is a telemetry problem, an exception here is an outage.
            cost_usd = 0.0

        self.recordings.put(
            digest,
            {
                "model": self.model,
                "purpose": purpose,
                "text": text,
                "raw_ref": raw_ref,
                "request_ref": request_ref,
                "cost_usd": cost_usd,
                "latency_ms": latency_ms,
                "usage": usage,
            },
        )

        return ModelResponse(
            text=text,
            model=self.model,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            raw_ref=raw_ref,
            request_ref=request_ref,
            from_recording=False,
            usage=usage,
        )
