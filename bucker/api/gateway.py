"""OpenAI-compatible inference gateway — "get an API directly, like OmniRoute".

The /v1 surface is a real inference gateway now, not a passthrough: the
routing engine (``bucker.gateway.routing``) owns provider selection,
fallback, retries, circuit breakers, quotas, and health. Callers send a
standard OpenAI-shaped request and receive a standard OpenAI-shaped
response (or SSE stream) — they never know which provider served it.

    curl http://localhost:8123/v1/chat/completions \
      -H "Authorization: Bearer $BUCKER_API_TOKEN" \
      -H "Content-Type: application/json" \
      -d '{"model":"deepseek/deepseek-v4-flash",
           "messages":[{"role":"user","content":"hello"}]}'

Also served here: GET /v1/models (what is actually routable today),
/health/live (process alive) and /health/ready (database reachable).

Every call is audited exactly like any other bucker task: a task row +
TaskCreated event + telemetry row, plus a durable row in the gateway_usage
ledger (quota/cost). Errors are normalized (spec §46): provider internals
never reach the caller.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from bucker.config import settings
from bucker.gateway.errors import GatewayError
from bucker.gateway.models import InferenceRequest, InferenceResponse
from bucker.gateway.quota import QuotaManager
from bucker.gateway.routing import RouterEngine, RoutingDecision

router = APIRouter(prefix="/v1", tags=["gateway"])
#: Root-level health endpoints (NOT under /v1) so orchestrators probe
#: /health/live and /health/ready (spec §36).
health_router = APIRouter(tags=["health"])
_bearer = HTTPBearer(auto_error=False)

#: Engine instance; tests swap this (like app._pool / app._store).
_engine: RouterEngine | None = None


class ChatMessage(BaseModel):
    role: str = Field(..., pattern="^(system|user|assistant|tool)$")
    content: str | list | None = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict] | None = None


class ChatRequest(BaseModel):
    model: str | None = None
    messages: list[ChatMessage] = Field(..., min_length=1)
    temperature: float = Field(0.0, ge=0.0, le=2.0)
    max_tokens: int | None = Field(None, ge=1)
    top_p: float | None = Field(None, ge=0.0, le=1.0)
    stream: bool = False
    tools: list[dict] | None = None
    tool_choice: str | dict | None = None
    response_format: dict | None = None
    request_id: str | None = None


# --------------------------------------------------------------------------
# Auth + engine
# --------------------------------------------------------------------------

def _check_gateway_auth(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    token = creds.credentials if creds else ""
    if not token or token != settings.api_token:
        raise HTTPException(
            status_code=401,
            detail="invalid or missing bearer token (use your BUCKER_API_TOKEN)",
        )


def _get_engine() -> RouterEngine:
    """Lazily build the shared engine (default registry + adapters + quota
    ledger backed by the app pool). Tests replace ``_engine`` directly."""
    global _engine
    if _engine is None:
        from bucker.api.app import _get_pool

        _engine = RouterEngine(quota=QuotaManager(pool_getter=_get_pool))
    return _engine


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------

@health_router.get("/health/live")
async def health_live() -> dict:
    """Process alive — no dependency checks."""
    return {"status": "ok"}


@health_router.get("/health/ready")
async def health_ready() -> dict:
    """Ready to accept traffic: database reachable. Provider health is NOT
    part of readiness — one provider being down must not take the whole
    gateway out (spec §36)."""
    from bucker.api.app import _get_pool

    try:
        pool = _get_pool()
        await pool.fetch("SELECT 1")
        return {"status": "ready"}
    except Exception:  # noqa: BLE001 — readiness probe
        raise HTTPException(status_code=503, detail="database unreachable") from None


@router.get("/models")
async def list_models(_: None = Depends(_check_gateway_auth)) -> dict:
    """Models the gateway can actually route to right now: registry entries
    whose provider adapter is available (key configured / local)."""
    engine = _get_engine()
    data = []
    for model in engine.registry.available():
        adapter = engine.adapters.get(model.provider)
        if adapter is not None and adapter.available():
            data.append({
                "id": model.canonical_id,
                "object": "model",
                "owned_by": model.provider,
                "context_window": model.context,
                "max_output_tokens": model.max_output,
                "free": model.free,
                "capabilities": sorted(model.capabilities),
            })
    return {"object": "list", "data": data}


@router.post("/chat/completions")
async def chat_completions(
    body: ChatRequest,
    response: Response,
    request: Request,
    _: None = Depends(_check_gateway_auth),
):
    from bucker.api.app import _get_pool, _get_store

    task_id = uuid.uuid4()
    request_id = body.request_id or str(task_id)
    tenant_id = request.headers.get("x-tenant-id", "default")
    response.headers["X-Request-Id"] = request_id

    pool = _get_pool()
    store = _get_store()
    event_id = await _audit_start(pool, store, task_id, _objective_of(body.messages))

    req = _to_inference_request(body, request_id, tenant_id)
    engine = _get_engine()

    if body.stream:
        # Planning errors (unknown model, impossible requirements, no
        # candidates) must surface as HTTP errors BEFORE the SSE headers
        # are sent; runtime failures become an SSE error event instead.
        decision = await engine.plan(req)

        async def sse() -> AsyncIterator[str]:
            async for frame in _stream_sse(
                engine, req, decision, task_id, request_id, event_id, pool
            ):
                yield frame

        return StreamingResponse(
            sse(), media_type="text/event-stream",
            headers={"X-Request-Id": request_id, "Cache-Control": "no-cache"},
        )

    started = time.monotonic()
    try:
        result: InferenceResponse = await engine.complete(req)
    except GatewayError as err:
        await _audit_telemetry(
            pool, event_id, task_id,
            model=req.model or "", latency_ms=0, cost_usd=None, usage=None,
        )
        # OpenAI-compatible error body at the TOP level (not {"detail": ...}):
        # clients parse {"error": {"message", "type", "code"}}.
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=err.status_code,
            content={
                "error": {
                    "message": err.safe,
                    "type": err.category,
                    "code": err.status_code,
                }
            },
        )

    latency_ms = int((time.monotonic() - started) * 1000)
    await _audit_telemetry(
        pool, event_id, task_id,
        model=result.model,
        latency_ms=latency_ms,
        cost_usd=result.usage.get("cost_usd"),
        usage=result.usage,
    )
    return _openai_response(task_id, result)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _to_inference_request(
    body: ChatRequest, request_id: str, tenant_id: str
) -> InferenceRequest:
    return InferenceRequest(
        request_id=request_id,
        tenant_id=tenant_id,
        purpose="gateway",
        messages=[m.model_dump() for m in body.messages],
        model=body.model,
        temperature=body.temperature,
        max_tokens=body.max_tokens,
        top_p=body.top_p,
        response_format=body.response_format,
        tools=body.tools,
        tool_choice=body.tool_choice,
        stream=body.stream,
    )


def _objective_of(messages: list[ChatMessage]) -> str:
    last = messages[-1]
    content = last.content
    if isinstance(content, str):
        return content[:120] or "gateway call"
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("text"):
                return str(part["text"])[:120]
        return "multimodal gateway call"
    return "gateway call"


async def _audit_start(pool, store, task_id: uuid.UUID, objective: str) -> int | None:
    """Audit trail for one gateway call, exactly like every other bucker
    task: a tasks row + a TaskCreated event. Order matters — events.task_id
    is an FK to tasks.id, so the tasks row MUST exist before the event
    append. Telemetry (record_model_call) then references the EVENT id
    (telemetry.event_id is an FK to events.id), so the return value is the
    event id. Failure never breaks the request."""
    from bucker.core.events import EventType

    try:
        await pool.fetchrow(
            "INSERT INTO tasks (id, status, objective, task_type, created_at) "
            "VALUES ($1, 'in_progress', $2, 'gateway', now()) "
            "ON CONFLICT (id) DO NOTHING",
            task_id, objective,
        )
        event = await store.append(
            task_id=task_id,
            event_type=EventType.TASK_CREATED,
            payload={"objective": objective, "task_type": "gateway"},
        )
        return event.id if event else None
    except Exception:  # noqa: BLE001 — audit must never break the call
        return None


async def _audit_telemetry(
    pool, event_id: int | None, task_id: uuid.UUID,
    *, model: str, latency_ms: int, cost_usd: float | None, usage: dict | None,
) -> None:
    """Telemetry row (the dashboard's cost view). Never breaks the call."""
    try:
        from bucker.core.telemetry import record_model_call

        conn = await pool.acquire()
        try:
            await record_model_call(
                conn,
                event_id=event_id or 0,
                task_id=task_id,
                model=model,
                latency_ms=latency_ms,
                cost_usd=cost_usd,
                purpose="gateway",
                usage=usage,
            )
        finally:
            await pool.release(conn)
    except Exception:  # noqa: BLE001
        pass


def _openai_response(task_id: uuid.UUID, result: InferenceResponse) -> dict:
    """Canonical InferenceResponse -> OpenAI non-streaming shape."""
    message: dict[str, Any] = {"role": "assistant", "content": result.content}
    if result.tool_calls:
        message["tool_calls"] = [
            {
                "id": tc["id"],
                "type": "function",
                "function": {"name": tc["name"], "arguments": tc["arguments"]},
            }
            for tc in result.tool_calls
        ]
    return {
        "id": f"chatcmpl-{task_id}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": result.model,
        "provider": result.provider,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": result.finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": result.usage.get("prompt_tokens", 0),
            "completion_tokens": result.usage.get("completion_tokens", 0),
            "total_tokens": result.usage.get("total_tokens", 0),
        },
        "cost_usd": result.usage.get("cost_usd"),
        "task_id": str(task_id),
    }


async def _stream_sse(
    engine: RouterEngine,
    req: InferenceRequest,
    decision: RoutingDecision,
    task_id: uuid.UUID,
    request_id: str,
    event_id: int | None,
    pool,
) -> AsyncIterator[str]:
    """Canonical stream events -> OpenAI SSE chunks + final audit."""
    model = decision.candidates[0] if decision.candidates else (req.model or "")
    created = int(time.time())
    usage_final: dict | None = None
    started = time.monotonic()

    def chunk(choices: list[dict], **extra: Any) -> str:
        payload = {
            "id": f"chatcmpl-{task_id}",
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            **extra,
            "choices": choices,
        }
        return f"data: {json.dumps(payload)}\n\n"

    try:
        async for ev in engine.stream(req, decision):
            if ev["type"] == "text_delta":
                yield chunk(
                    [{"index": 0, "delta": {"content": ev["text"]}, "finish_reason": None}]
                )
            elif ev["type"] == "tool_call_delta":
                yield chunk(
                    [{
                        "index": 0,
                        "delta": {
                            "tool_calls": [{
                                "index": ev["index"],
                                "id": ev["id"] or None,
                                "function": {
                                    "name": ev["name"] or None,
                                    "arguments": ev["arguments"],
                                },
                            }]
                        },
                        "finish_reason": None,
                    }]
                )
            elif ev["type"] == "finish":
                yield chunk(
                    [{
                        "index": 0,
                        "delta": {},
                        "finish_reason": ev.get("finish_reason") or "stop",
                    }]
                )
            elif ev["type"] == "usage":
                usage_final = ev
                yield chunk(
                    [],
                    usage={
                        "prompt_tokens": ev.get("prompt_tokens", 0),
                        "completion_tokens": ev.get("completion_tokens", 0),
                        "total_tokens": (
                            ev.get("prompt_tokens", 0)
                            + ev.get("completion_tokens", 0)
                        ),
                    },
                )
            elif ev["type"] == "error":
                yield chunk(
                    [{"index": 0, "delta": {}, "finish_reason": "error"}]
                )
    finally:
        latency_ms = int((time.monotonic() - started) * 1000)
        await _audit_telemetry(
            pool, event_id, task_id,
            model=model,
            latency_ms=latency_ms,
            cost_usd=(usage_final or {}).get("cost_usd"),
            usage=usage_final,
        )
    yield "data: [DONE]\n\n"
