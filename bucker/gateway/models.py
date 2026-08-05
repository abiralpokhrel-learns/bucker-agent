"""Canonical inference objects (spec §3, §17, §18).

The gateway speaks ONE internal shape — ``InferenceRequest`` in,
``InferenceResponse`` / stream events out — and the provider adapters are
the only place that shape meets a provider's API. Nothing else in the
system (API layer, routing engine, agent callers) ever sees provider JSON.

Messages use the OpenAI conversation shape on purpose: it is the de-facto
canonical form every adapter and agent framework already understands.
``content`` may be a plain string or a list of parts (multimodal) — the
adapters pass it through to providers that support it.

Canonical tool calls are ``{"id", "name", "arguments"}`` with ``arguments``
always a JSON string, regardless of how the provider represents them.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------
# Routing requirements / policies
# --------------------------------------------------------------------------

#: Routing strategies the engine understands (spec §9). Configurable per
#: request via ``InferenceRequest.policy`` or globally via
#: BUCKER_GATEWAY_POLICY.
ROUTING_POLICIES = (
    "priority",     # configured chain order (BUCKER_MODEL, then fallbacks)
    "cost",         # cheapest eligible first (free models before paid)
    "latency",      # lowest recent average latency first
    "balanced",     # weighted score: priority + cost + latency + reliability
    "free_only",    # only legitimately free providers/models
    "local_first",  # local inference first, then free, then paid
)

#: Capability names used by the registry and by requirements.
CAP_TOOLS = "tools"
CAP_STREAMING = "streaming"
CAP_VISION = "vision"
CAP_REASONING = "reasoning"
CAP_CODING = "coding"
CAP_STRUCTURED = "structured_output"


@dataclass(frozen=True, slots=True)
class InferenceRequest:
    """One canonical inference request, provider-neutral (spec §3)."""

    messages: list[dict]                 # role/content(/tool_calls/tool_call_id)
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    tenant_id: str = "default"
    purpose: str = "gateway"

    # --- model selection ------------------------------------------------
    #: Preferred canonical model id ("provider/model_id"). The engine tries
    #: it first, then falls back through other eligible candidates. If it
    #: fails the request's HARD requirements the engine rejects early
    #: rather than silently substituting (spec §28).
    model: str | None = None

    # --- capabilities / constraints -------------------------------------
    min_context: int | None = None       # minimum context window in tokens
    free_only: bool = False              # only free-tier models
    local_first: bool = False            # prefer local inference
    #: Cost ceiling: maximum combined USD per 1M tokens (input + output
    #: prices added). Models whose per-1M price exceeds it are excluded.
    #: Unknown-price models are never excluded (their cost cannot be
    #: proven over budget) — the same fail-safe as the rest of the system.
    max_cost_usd: float | None = None
    policy: str | None = None            # one of ROUTING_POLICIES

    # --- sampling --------------------------------------------------------
    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None
    response_format: dict | None = None

    # --- tools -----------------------------------------------------------
    tools: list[dict] | None = None
    tool_choice: str | dict | None = None

    # --- transport --------------------------------------------------------
    stream: bool = False
    timeout_s: float | None = None       # per-attempt cap
    deadline_s: float | None = None      # total budget across all attempts

    metadata: dict = field(default_factory=dict)

    @property
    def needs_tools(self) -> bool:
        return bool(self.tools)

    @property
    def needs_streaming(self) -> bool:
        return self.stream

    @property
    def needs_vision(self) -> bool:
        """True when any message content is a multimodal part list."""
        for msg in self.messages:
            content = msg.get("content")
            if isinstance(content, list):
                return True
        return False


@dataclass(frozen=True, slots=True)
class InferenceResponse:
    """Canonical, provider-neutral result (spec §3)."""

    request_id: str
    model: str                       # canonical id actually served
    provider: str
    content: str
    tool_calls: list[dict] | None    # [{id, name, arguments(json str)}]
    finish_reason: str
    usage: dict                      # prompt/completion/total tokens, cost_usd
    latency_ms: int
    attempts: int                    # how many candidate attempts it took
    from_fallback: bool = False
    #: Verbatim provider payload, for archiving/audit only (the ModelRouter
    #: stores it content-addressed for replay). NOT part of the API contract
    #: — never returned to callers.
    raw: dict | None = None


# --------------------------------------------------------------------------
# Streaming (spec §17): one canonical event shape, normalized by adapters.
# --------------------------------------------------------------------------

#: Event ``type`` values the engine/API emit:
#:   text_delta       {"text": "..."}                        — content chunk
#:   tool_call_delta  {"index": 0, "id": "...", "name": "...",
#:                     "arguments": "..."}                   — incremental
#:   finish           {"finish_reason": "stop"}              — choice done
#:   usage            {"prompt_tokens": n, "completion_tokens": n,
#:                     "total_tokens": n, "cost_usd": f}     — final usage
#:   error            {"error_type": "...", "message": "..."}— fatal, stream
#:                                                             ends after it
STREAM_EVENT_TYPES = ("text_delta", "tool_call_delta", "finish", "usage", "error")


def stream_event(type_: str, **data: Any) -> dict:
    """Build one canonical stream event dict."""
    assert type_ in STREAM_EVENT_TYPES, f"unknown stream event type {type_!r}"
    return {"type": type_, **data}


def usage(prompt_tokens: int, completion_tokens: int, cost_usd: float | None) -> dict:
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "cost_usd": cost_usd,
    }


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def estimate_tokens(text: str) -> int:
    """Rough ~4-char-per-token estimate for providers without usage data."""
    return max(1, len(text) // 4)


def estimate_messages_tokens(messages: list[dict]) -> int:
    total = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            for part in content:
                text = part.get("text") if isinstance(part, dict) else None
                if text:
                    total += estimate_tokens(text)
    return total


def now_unix() -> int:
    return int(time.time())
