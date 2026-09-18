"""Canonical engine events to OpenAI SSE; engine owns fallback safety."""

import asyncio
import json
import time
from types import SimpleNamespace

from bucker.gateway.errors import GatewayError, GatewayTimeoutError

from .diagnostics import public_error


def frame(payload):
    return "data: " + json.dumps(payload) + "\n\n"


async def stream_frames(engine, req, decision, ledger, include_usage=False):
    created = int(time.time())
    usage = None
    error = None
    served = None
    seen_tools = set()
    pending_finish = None
    ttft_ms: int | None = None
    stream_start = time.monotonic()

    def chunk(choices, **extra):
        return frame(
            {
                "id": f"chatcmpl-{req.request_id}",
                "object": "chat.completion.chunk",
                "created": created,
                "model": "desktop-auto",
                "choices": choices,
                **extra,
            }
        )

    def choice(delta, finish=None):
        return [{"index": 0, "delta": delta, "finish_reason": finish}]

    try:
        # RouterEngine checks its clock on incoming events; this outer timer
        # also bounds a provider that goes silent between chunks.
        async with asyncio.timeout(engine.deadline_s):
            async for event in engine.stream(req, decision):
                kind = event["type"]
                if kind in ("text_delta", "tool_call_delta"):
                    if ttft_ms is None:
                        ttft_ms = int((time.monotonic() - stream_start) * 1000)
                    # Failed attempts are appended before the next candidate.
                    served = decision.candidates[len(decision.attempts)]
                    if kind == "text_delta":
                        yield chunk(choice({"content": event["text"]}))
                    else:
                        index = event["index"]
                        call = {"index": index, "function": {"arguments": event["arguments"]}}
                        # Shared adapter reports accumulated id/name each time;
                        # OpenAI clients concatenate them. Send identity once.
                        if index not in seen_tools:
                            call.update(id=event["id"], type="function")
                            call["function"]["name"] = event["name"]
                            seen_tools.add(index)
                        yield chunk(choice({"tool_calls": [call]}))
                elif kind == "finish":
                    # An empty candidate can finish then fall back. Don't
                    # forward its terminal frame until selection is final.
                    pending_finish = event.get("finish_reason") or "stop"
                elif kind == "usage":
                    if event.get("reported", False):
                        usage = {
                            "prompt_tokens": event.get("prompt_tokens", 0),
                            "completion_tokens": event.get("completion_tokens", 0),
                            "cost_usd": None,
                        }
                        if event.get("cached_tokens") is not None:
                            usage["cached_tokens"] = event.get("cached_tokens")
                        if event.get("ttft_ms") is not None:
                            usage["ttft_ms"] = event.get("ttft_ms")
                        elif ttft_ms is not None:
                            usage["ttft_ms"] = ttft_ms
                        usage["total_tokens"] = (
                            usage["prompt_tokens"] + usage["completion_tokens"]
                        )
                    else:
                        usage = None
                elif kind == "error":
                    # Interrupted mid-flight (partial tool args already sent):
                    # the engine guarantees no fallback happened and no finish
                    # was emitted. Surface the honest interrupted message so
                    # the UI shows "attempt didn't finish" instead of silently
                    # continuing or claiming a retry started.
                    if event.get("interrupted"):
                        error = public_error(event["error_type"], decision.attempts)
                        # Prefer the engine's honest message over the generic
                        # provider hint when a partial proposal was discarded.
                        honest = event.get("honest_message")
                        if honest:
                            error.safe = honest
                    else:
                        error = public_error(event["error_type"], decision.attempts)
                    break
    except TimeoutError:
        error = GatewayTimeoutError("stream deadline exceeded")
    except asyncio.CancelledError:
        error = GatewayError("client disconnected")
        error.category = "client_disconnected"
        raise
    except Exception:
        # Never log/serialize raw provider payloads or Python exception text.
        error = GatewayError("stream failed")
    finally:
        if error is not None:
            error = public_error(error.category, decision.attempts)
        served = decision.selected or served
        model = engine.registry.get(served) if served else None
        result = (
            SimpleNamespace(
                model=served,
                provider=model.provider if model else None,
                usage=usage,
                attempts=len(decision.attempts) + int(error is None and served is not None),
            )
            if served
            else None
        )
        ledger.record(req, result=result, decision=decision, error=error)

    if error:
        yield frame(
            {
                "error": {
                    "type": error.category,
                    "message": error.safe,
                    "code": error.status_code,
                }
            }
        )
    else:
        yield chunk(choice({}, pending_finish or "stop"))
        if include_usage and usage is not None:
            yield chunk(
                [],
                usage={
                    k: usage[k] for k in ("prompt_tokens", "completion_tokens", "total_tokens")
                },
            )
    yield "data: [DONE]\n\n"
