"""OpenAI-compatible gateway — "get an API directly, like OmniRoute".

One endpoint, your BUCKER_API_TOKEN as the key, and the existing free-first
chain (DeepSeek → Ollama → OpenRouter free) does the routing with
auto-fallback. Every call is audited: a gateway task row + event + model
telemetry, exactly like any other bucker task.

    curl http://localhost:8123/v1/chat/completions \\
      -H "Authorization: Bearer $BUCKER_API_TOKEN" \\
      -H "Content-Type: application/json" \\
      -d '{"model":"deepseek/deepseek-v4-flash",
           "messages":[{"role":"user","content":"hello"}]}'
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from bucker.config import settings
from bucker.router.client import ModelRouter

router = APIRouter(prefix="/v1", tags=["gateway"])
_bearer = HTTPBearer(auto_error=False)


class ChatMessage(BaseModel):
    role: str = Field(..., pattern="^(system|user|assistant)$")
    content: str


class ChatRequest(BaseModel):
    model: str | None = None
    messages: list[ChatMessage] = Field(..., min_length=1)
    temperature: float = Field(0.0, ge=0.0, le=2.0)
    max_tokens: int | None = Field(None, ge=1)
    stream: bool = False


def _tokens(text: str) -> int:
    """Rough estimate (~4 chars/token) — usage fields are estimates."""
    return max(1, len(text) // 4)


def _check_gateway_auth(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    token = creds.credentials if creds else ""
    if not token or token != settings.api_token:
        raise HTTPException(
            status_code=401,
            detail="invalid or missing bearer token "
                   "(use your BUCKER_API_TOKEN)",
        )


@router.get("/models")
async def list_models(_: None = Depends(_check_gateway_auth)) -> dict:
    models = [settings.model, *settings.model_fallbacks]
    return {
        "object": "list",
        "data": [
            {"id": m, "object": "model", "owned_by": "bucker-agent"}
            for m in models
        ],
    }


@router.post("/chat/completions")
async def chat_completions(
    body: ChatRequest,
    _: None = Depends(_check_gateway_auth),
) -> dict[str, Any]:
    from bucker.api.app import _get_pool, _get_store
    from bucker.core.events import EventType
    from bucker.core.eventstore import EventStore

    if body.stream:
        raise HTTPException(status_code=400, detail="streaming not supported yet")

    messages = [m.model_dump() for m in body.messages]
    task_id = uuid.uuid4()

    # --- audit trail: a gateway task row + TaskCreated event ----------------
    store: EventStore = _get_store()
    pool = _get_pool()
    objective = messages[-1]["content"][:120] if messages else "gateway call"
    try:
        row = await pool.fetchrow(
            "INSERT INTO tasks (id, status, objective, task_type, created_at) "
            "VALUES ($1, 'in_progress', $2, 'gateway', now()) "
            "ON CONFLICT (id) DO NOTHING RETURNING id",
            task_id, objective,
        )
        event_id = row["id"] if row else None
        await store.append(
            task_id=task_id,
            event_type=EventType.TASK_CREATED,
            payload={"objective": objective, "task_type": "gateway"},
        )
    except Exception as exc:  # noqa: BLE001 - audit must not 500 the call
        raise HTTPException(status_code=502, detail=f"audit trail failed: {exc}") from exc

    # --- route through the provider chain -----------------------------------
    started = time.monotonic()
    try:
        client = ModelRouter()
        resp = await client.complete(
            messages,
            purpose="gateway",
            temperature=body.temperature,
            max_tokens=body.max_tokens,
        )
    except Exception as exc:  # noqa: BLE001 - expose honest failure
        raise HTTPException(
            status_code=502, detail=f"provider chain failed: {exc}"
        ) from exc

    latency_ms = int((time.monotonic() - started) * 1000)

    # --- telemetry ------------------------------------------------------------
    try:
        conn = await pool.acquire()
        try:
            from bucker.core.telemetry import record_model_call

            await record_model_call(
                conn,
                event_id=event_id or 0,
                task_id=task_id,
                model=resp.model,
                latency_ms=latency_ms,
                cost_usd=resp.cost_usd,
                purpose="gateway",
            )
        finally:
            await pool.release(conn)
    except Exception:  # noqa: BLE001
        pass  # telemetry failure must not break the API response

    # --- OpenAI-shaped response ---------------------------------------------
    content = resp.text or ""
    return {
        "id": f"chatcmpl-{task_id}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": resp.model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": sum(_tokens(m["content"]) for m in messages),
            "completion_tokens": _tokens(content),
            "total_tokens": sum(_tokens(m["content"]) for m in messages)
                            + _tokens(content),
        },
        "cost_usd": resp.cost_usd,
        "task_id": str(task_id),
    }
