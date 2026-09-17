"""Small OpenAI surface, independent of bucker.api.gateway/app."""

import time
from collections import deque
from dataclasses import replace
from typing import Literal

from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field

from bucker.gateway.errors import GatewayError
from bucker.gateway.models import InferenceRequest


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: str | None = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict] | None = None


class StreamOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    include_usage: bool = False


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    model: str | None = None
    messages: list[Message] = Field(min_length=1)
    temperature: float | None = Field(None, ge=0, le=2)
    top_p: float | None = Field(None, ge=0, le=1)
    max_tokens: int | None = Field(None, gt=0)
    tools: list[dict] | None = None
    tool_choice: str | dict | None = None
    response_format: dict | None = None
    stream: bool = False
    stream_options: StreamOptions | None = None

    def inference(self):
        return InferenceRequest(
            model=None if self.model in (None, "desktop-auto") else self.model,
            messages=[m.model_dump(exclude_unset=True) for m in self.messages],
            free_only=True,
            policy="free_only",
            purpose="desktop",
            temperature=self.temperature,
            top_p=self.top_p,
            max_tokens=self.max_tokens,
            tools=self.tools,
            tool_choice=self.tool_choice,
            response_format=self.response_format,
            stream=self.stream,
        )


class SessionUsage:
    """Bounded metadata only: no prompts, tool arguments, credentials, or disk."""

    def __init__(self):
        self.recent = deque(maxlen=200)
        self.requests = self.errors = 0
        self.prompt_tokens = self.completion_tokens = 0

    def record(self, req, *, result=None, decision=None, error=None):
        self.requests += 1
        self.errors += int(error is not None)
        usage = result.usage if result else None
        if usage:
            self.prompt_tokens += usage.get("prompt_tokens", 0)
            self.completion_tokens += usage.get("completion_tokens", 0)
        attempts = decision.attempts if decision else getattr(error, "attempts", [])
        self.recent.append(
            {
                "request_id": req.request_id,
                "provider": result.provider if result else None,
                "served_model": result.model if result else None,
                "outcome": "error" if error else "success",
                "error_type": error.category if error else None,
                "attempt_count": result.attempts if result else len(attempts),
                "fallback_attempts": [dict(a) for a in attempts],
                "usage": usage,
            }
        )

    def snapshot(self):
        return {
            "scope": "session",
            "requests": self.requests,
            "errors": self.errors,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
            "remaining_quota": None,
            "cost_usd": None,
            "recent_requests": list(self.recent),
        }


def completion(result):
    message = {"role": "assistant", "content": result.content}
    if result.tool_calls:
        message["tool_calls"] = [
            {
                "id": t["id"],
                "type": "function",
                "function": {"name": t["name"], "arguments": t["arguments"]},
            }
            for t in result.tool_calls
        ]
    return {
        "id": f"chatcmpl-{result.request_id}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "desktop-auto",
        "choices": [{"index": 0, "message": message, "finish_reason": result.finish_reason}],
        "usage": (
            {k: result.usage[k] for k in ("prompt_tokens", "completion_tokens", "total_tokens")}
            if result.usage is not None
            else None
        ),
    }


def install_routes(app, engine, error_response):
    from fastapi.responses import JSONResponse

    ledger = SessionUsage()
    app.state.ledger = ledger

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        return error_response(
            422, "invalid_request_error", "invalid or unsupported request fields"
        )

    @app.get("/usage")
    async def usage():
        return ledger.snapshot()

    @app.post("/v1/chat/completions", response_model=None)
    async def chat(body: ChatRequest):
        req = body.inference()
        try:
            if body.stream:
                from fastapi.responses import StreamingResponse

                from .streaming import stream_frames

                decision = await engine.plan(req)
                return StreamingResponse(
                    stream_frames(
                        engine,
                        req,
                        decision,
                        ledger,
                        bool(body.stream_options and body.stream_options.include_usage),
                    ),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Request-Id": req.request_id},
                )
            result, decision = await engine.complete_with_decision(req)
        except GatewayError as exc:
            from .diagnostics import public_error

            error = public_error(exc.category, getattr(exc, "attempts", ()))
            ledger.record(req, error=exc)
            return error_response(error.status_code, error.category, error.safe)
        if not (result.raw or {}).get("usage"):
            result = replace(result, usage=None)
        ledger.record(req, result=result, decision=decision)
        return JSONResponse(
            completion(result),
            headers={
                "X-Request-Id": req.request_id,
                "X-Served-Model": result.model,
                "X-Served-Provider": result.provider,
            },
        )
