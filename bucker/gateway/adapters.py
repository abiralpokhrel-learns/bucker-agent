"""Provider adapters — the ONLY place canonical objects meet provider APIs
(spec §4, §17, §46).

Every provider implements the same internal interface:

    complete(req, model_id) -> RawCompletion        (non-streaming)
    stream(req, model_id)   -> AsyncIterator[dict]  (normalized events)
    available() / health()

The adapter owns: endpoint + authentication, payload translation, response
parsing, streaming normalization, and error mapping into the taxonomy
(``bucker.gateway.errors``). Adapters NEVER make routing decisions — the
routing engine decides where requests go (spec §8). Capability differences
between providers live in the model registry, not here.

Three real adapters ship today — DeepSeek (direct), OpenRouter, and Ollama
(local) — all OpenAI-compatible, which is the de-facto standard shape.
``SimulatedProvider`` is a scripted provider for hermetic tests (spec §48):
it can produce success, tool calls, streaming, 429s, 5xx, timeouts,
malformed responses, auth failures, and quota exhaustion without touching
the network.
"""

from __future__ import annotations

import abc
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from bucker.config import settings
from bucker.gateway.errors import (
    AuthenticationError,
    ContextLengthError,
    GatewayError,
    GatewayTimeoutError,
    InvalidRequestError,
    ModelUnavailableError,
    ProviderUnavailableError,
    QuotaExceededError,
    RateLimitError,
)
from bucker.gateway.models import InferenceRequest, stream_event


@dataclass(slots=True)
class RawCompletion:
    """A provider response, still in canonical shape but un-costed."""

    text: str
    tool_calls: list[dict] | None   # [{id, name, arguments(json str)}]
    finish_reason: str
    usage: dict                     # prompt_tokens / completion_tokens
    raw: dict = field(default_factory=dict)


class ProviderAdapter(abc.ABC):
    """The common internal provider interface (spec §4)."""

    name: str = ""

    @abc.abstractmethod
    def available(self) -> bool:
        """True if this provider could be used right now (key configured,
        endpoint local). Cheap, synchronous, never probes the network."""

    async def health(self) -> bool:
        """Cheap liveness probe (defaults to availability)."""
        return self.available()

    @abc.abstractmethod
    async def complete(self, req: InferenceRequest, model_id: str) -> RawCompletion:
        """One non-streaming completion. Raises taxonomy errors on failure."""

    def stream(self, req: InferenceRequest, model_id: str) -> AsyncIterator[dict]:
        """Normalized stream of events (spec §17): text_delta, tool_call_delta,
        finish, usage. Raises taxonomy errors on failure (before OR mid-stream;
        the engine decides whether a mid-stream failure is recoverable)."""
        raise NotImplementedError(
            f"{self.name} does not implement streaming — the registry's "
            "capability filter should have excluded it from stream requests"
        )


# --------------------------------------------------------------------------
# OpenAI-compatible adapter (base for DeepSeek / OpenRouter / Ollama / vLLM)
# --------------------------------------------------------------------------

class OpenAICompatAdapter(ProviderAdapter):
    """Shared implementation for OpenAI-compatible /chat/completions APIs.

    Subclasses declare ``name``, ``base_url``, and ``_key`` (the settings
    attribute holding the API key, or None for keyless endpoints like
    Ollama). Payload translation, response parsing, streaming
    normalization, and error mapping are shared here.
    """

    name = "openai_compat"
    base_url = ""
    _key: str | None = None

    def __init__(self, *, timeout_s: float | None = None) -> None:
        self._timeout_s = timeout_s or settings.gateway_timeout_s
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------ auth --
    @property
    def api_key(self) -> str | None:
        if self._key is None:
            return None
        return getattr(settings, self._key, "") or None

    def available(self) -> bool:
        return self.api_key is not None if self._key is not None else True

    def _client_for(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers=headers,
                timeout=httpx.Timeout(self._timeout_s),
            )
        return self._client

    # --------------------------------------------------------- payload --
    def _payload(self, req: InferenceRequest, model_id: str) -> dict[str, Any]:
        """Canonical InferenceRequest -> OpenAI request body."""
        body: dict[str, Any] = {"model": model_id, "messages": req.messages}
        if req.temperature is not None:
            body["temperature"] = req.temperature
        if req.max_tokens is not None:
            body["max_tokens"] = req.max_tokens
        if req.top_p is not None:
            body["top_p"] = req.top_p
        if req.response_format is not None:
            body["response_format"] = req.response_format
        if req.tools:
            body["tools"] = req.tools
        if req.tool_choice:
            body["tool_choice"] = req.tool_choice
        return body

    # ------------------------------------------------------ non-stream --
    async def complete(self, req: InferenceRequest, model_id: str) -> RawCompletion:
        try:
            resp = await self._client_for().post(
                "/chat/completions", json=self._payload(req, model_id)
            )
        except httpx.TimeoutException as exc:
            raise GatewayTimeoutError(
                f"{self.name} timed out", provider=self.name, model=model_id
            ) from exc
        except httpx.RequestError as exc:
            raise ProviderUnavailableError(
                f"{self.name} unreachable ({type(exc).__name__})",
                provider=self.name,
                model=model_id,
            ) from exc

        if resp.status_code >= 400:
            raise self._map_error(resp.status_code, resp.text, model_id)

        try:
            data = resp.json()
            choice = data["choices"][0]
            message = choice.get("message") or {}
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderUnavailableError(
                f"{self.name} returned a malformed response",
                provider=self.name,
                model=model_id,
            ) from exc

        text = message.get("content") or ""
        tool_calls = None
        raw_tool_calls = message.get("tool_calls")
        if raw_tool_calls:
            tool_calls = [
                {
                    "id": tc.get("id", ""),
                    "name": (tc.get("function") or {}).get("name", ""),
                    "arguments": (tc.get("function") or {}).get("arguments", "{}"),
                }
                for tc in raw_tool_calls
            ]
        # Empty content with no tool call is a failed attempt, not a
        # success: reasoning models can spend the whole output budget on
        # reasoning_content and return an empty message. (The ENGINE is the
        # single source of truth for this guard — see RouterEngine._attempt
        # — so every adapter and the simulated provider behave identically.)
        usage_ = data.get("usage") or {}
        return RawCompletion(
            text=text,
            tool_calls=tool_calls,
            finish_reason=(
                choice.get("finish_reason") or ("tool_calls" if tool_calls else "stop")
            ),
            usage={
                "prompt_tokens": usage_.get("prompt_tokens", 0),
                "completion_tokens": usage_.get("completion_tokens", 0),
            },
            raw=data,
        )

    # --------------------------------------------------------- stream --
    async def stream(self, req: InferenceRequest, model_id: str) -> AsyncIterator[dict]:
        payload = self._payload(req, model_id)
        payload["stream"] = True
        text_parts: list[str] = []
        tool_acc: dict[int, dict[str, str]] = {}
        prompt_tokens = completion_tokens = 0
        finish_reason: str | None = None

        try:
            client = self._client_for()
            async with client.stream("POST", "/chat/completions", json=payload) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    raise self._map_error(
                        resp.status_code, body.decode("utf-8", "replace"), model_id
                    )
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if chunk == "[DONE]":
                        break
                    try:
                        data = json.loads(chunk)
                    except ValueError:
                        continue  # keepalive / comment lines are noise
                    events, fr = self._normalize_chunk(data, text_parts, tool_acc)
                    for ev in events:
                        yield ev
                    if fr:
                        finish_reason = fr
                    usage_ = data.get("usage")
                    if usage_:
                        prompt_tokens = usage_.get("prompt_tokens", 0)
                        completion_tokens = usage_.get("completion_tokens", 0)
        except httpx.TimeoutException as exc:
            raise GatewayTimeoutError(
                f"{self.name} stream timed out", provider=self.name, model=model_id
            ) from exc
        except httpx.RequestError as exc:
            raise ProviderUnavailableError(
                f"{self.name} stream interrupted ({type(exc).__name__})",
                provider=self.name,
                model=model_id,
            ) from exc

        if tool_acc:
            finish_reason = finish_reason or "tool_calls"
            yield stream_event(
                "finish",
                finish_reason=finish_reason,
                tool_calls=[
                    {
                        "id": acc["id"],
                        "name": acc["name"],
                        "arguments": acc["arguments"] or "{}",
                    }
                    for acc in tool_acc.values()
                ],
            )
        else:
            yield stream_event("finish", finish_reason=finish_reason or "stop", tool_calls=None)
        yield stream_event(
            "usage", prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
        )

    @staticmethod
    def _normalize_chunk(
        data: dict, text_parts: list[str], tool_acc: dict[int, dict[str, str]]
    ) -> tuple[list[dict], str | None]:
        """One OpenAI SSE chunk -> canonical events. Returns (events, finish_reason)."""
        events: list[dict] = []
        finish_reason: str | None = None
        for choice in data.get("choices") or []:
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}
            text = delta.get("content")
            if text:
                text_parts.append(text)
                events.append(stream_event("text_delta", text=text))
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                acc = tool_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                fn = tc.get("function") or {}
                if tc.get("id"):
                    acc["id"] = tc["id"]
                if fn.get("name"):
                    acc["name"] = fn["name"]
                arg_delta = fn.get("arguments") or ""
                if arg_delta:
                    acc["arguments"] += arg_delta
                events.append(
                    stream_event(
                        "tool_call_delta",
                        index=idx,
                        id=acc["id"],
                        name=acc["name"],
                        arguments=arg_delta,
                    )
                )
        return events, finish_reason

    # ------------------------------------------------- error mapping --
    def _map_error(self, status: int, body: str, model_id: str) -> GatewayError:
        """Provider HTTP error -> taxonomy error (spec §46-47).

        The taxonomy drives routing: auth stops this credential, 429 may
        trigger cooldown, 5xx retries, model-not-found removes the model,
        invalid requests come back to the caller. Never embed raw provider
        bodies in the safe message.
        """
        detail = ""
        try:
            err = json.loads(body).get("error") or {}
            if isinstance(err, dict):
                detail = str(err.get("message", ""))
        except ValueError:
            pass
        low = detail.lower()

        if status in (401, 403):
            return AuthenticationError(
                f"{self.name} rejected the API key (HTTP {status})",
                provider=self.name,
                model=model_id,
            )
        if status == 429:
            if any(k in low for k in ("insufficient credits", "quota", "limit reached")):
                return QuotaExceededError(
                    f"{self.name} quota/entitlement exhausted (HTTP 429)",
                    provider=self.name,
                    model=model_id,
                )
            return RateLimitError(
                f"{self.name} rate limited (HTTP 429)", provider=self.name, model=model_id
            )
        if status == 404:
            return ModelUnavailableError(
                f"{self.name} does not serve model {model_id!r}",
                provider=self.name,
                model=model_id,
            )
        if status == 400:
            context_hints = (
                "context length", "maximum context", "context window", "token limit"
            )
            if any(k in low for k in context_hints):
                return ContextLengthError(
                    f"{self.name}: request exceeds context window",
                    provider=self.name,
                    model=model_id,
                )
            return InvalidRequestError(
                f"{self.name} rejected the request: {detail[:200]}",
                provider=self.name,
                model=model_id,
            )
        if status == 408:
            return GatewayTimeoutError(
                f"{self.name} timed out (HTTP 408)", provider=self.name, model=model_id
            )
        return ProviderUnavailableError(
            f"{self.name} returned HTTP {status}",
            provider=self.name,
            model=model_id,
        )


# --------------------------------------------------------------------------
# Concrete adapters
# --------------------------------------------------------------------------

class DeepSeekAdapter(OpenAICompatAdapter):
    """DeepSeek official API (OpenAI-compatible; the user's paid default)."""

    name = "deepseek"
    _key = "deepseek_api_key"

    def __init__(self, *, timeout_s: float | None = None) -> None:
        super().__init__(timeout_s=timeout_s)
        base = settings.deepseek_base_url.rstrip("/")
        self.base_url = base if base.endswith("/v1") else f"{base}/v1"


class OpenRouterAdapter(OpenAICompatAdapter):
    """OpenRouter (hosted catalog; the free-tier source)."""

    name = "openrouter"
    base_url = "https://openrouter.ai/api/v1"
    _key = "openrouter_api_key"


class OllamaAdapter(OpenAICompatAdapter):
    """Local Ollama (keyless, OpenAI-compatible endpoint)."""

    name = "ollama"
    base_url = "http://127.0.0.1:11434/v1"
    _key = None

    def available(self) -> bool:
        # No key concept: availability is decided by the routing attempt
        # (connection failure falls through to the next candidate). Local
        # inference being down must never take down a request.
        return True

    async def health(self) -> bool:
        try:
            resp = await self._client_for().get("/models", timeout=2.0)
            return resp.status_code == 200
        except Exception:  # noqa: BLE001 — probe must never raise
            return False


def default_adapters() -> dict[str, ProviderAdapter]:
    """The adapters the gateway knows by default."""
    return {
        "deepseek": DeepSeekAdapter(),
        "openrouter": OpenRouterAdapter(),
        "ollama": OllamaAdapter(),
    }


# --------------------------------------------------------------------------
# Simulated provider (hermetic tests / local demos — spec §48)
# --------------------------------------------------------------------------

_SCENARIOS = (
    "success", "tool_call", "stream", "stream_tool_call",
    "rate_limit", "quota_exhausted", "server_error", "timeout",
    "auth_error", "model_unavailable", "context_length", "malformed",
    "invalid_request",
)


class SimulatedProvider(ProviderAdapter):
    """Scripted provider: every failure mode without touching the network.

    ``script(model_id, scenario)`` sets behavior per model. Every invocation
    is recorded on ``.calls`` so tests can assert exact routing order.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[tuple[str, InferenceRequest]] = []
        self._scripts: dict[str, str] = {}

    def script(self, model_id: str, scenario: str) -> SimulatedProvider:
        assert scenario in _SCENARIOS, f"unknown scenario {scenario!r}"
        self._scripts[model_id] = scenario
        return self

    def available(self) -> bool:
        return True

    async def health(self) -> bool:
        return True

    async def complete(self, req: InferenceRequest, model_id: str) -> RawCompletion:
        self.calls.append((model_id, req))
        scenario = self._scripts.get(model_id, "success")
        if scenario in ("success", "stream"):
            return RawCompletion(
                text=f"hello from {self.name}/{model_id}",
                tool_calls=None,
                finish_reason="stop",
                usage={"prompt_tokens": 5, "completion_tokens": 9},
            )
        if scenario == "tool_call":
            return RawCompletion(
                text="",
                tool_calls=[
                    {
                        "id": f"call_{self.name}",
                        "name": "run_shell",
                        "arguments": json.dumps({"cmd": "echo hi"}),
                    }
                ],
                finish_reason="tool_calls",
                usage={"prompt_tokens": 5, "completion_tokens": 12},
            )
        raise self._raise_for(scenario, model_id)

    async def stream(self, req: InferenceRequest, model_id: str) -> AsyncIterator[dict]:
        self.calls.append((model_id, req))
        scenario = self._scripts.get(model_id, "stream")
        if scenario in ("stream", "success"):
            yield stream_event("text_delta", text="hello ")
            yield stream_event("text_delta", text=f"from {self.name}")
            yield stream_event("finish", finish_reason="stop", tool_calls=None)
            yield stream_event("usage", prompt_tokens=5, completion_tokens=9)
        elif scenario in ("stream_tool_call", "tool_call"):
            yield stream_event(
                "tool_call_delta", index=0, id=f"call_{self.name}",
                name="run_shell", arguments='{"cmd":',
            )
            yield stream_event(
                "tool_call_delta", index=0, id=f"call_{self.name}",
                name="run_shell", arguments='"echo hi"}',
            )
            yield stream_event(
                "finish", finish_reason="tool_calls",
                tool_calls=[
                    {"id": f"call_{self.name}", "name": "run_shell",
                     "arguments": '{"cmd": "echo hi"}'}
                ],
            )
            yield stream_event("usage", prompt_tokens=5, completion_tokens=12)
        else:
            raise self._raise_for(scenario, model_id)

    def _raise_for(self, scenario: str, model_id: str) -> GatewayError:
        factory = {
            "rate_limit": RateLimitError,
            "quota_exhausted": QuotaExceededError,
            "server_error": ProviderUnavailableError,
            "timeout": GatewayTimeoutError,
            "auth_error": AuthenticationError,
            "model_unavailable": ModelUnavailableError,
            "context_length": ContextLengthError,
            "malformed": ProviderUnavailableError,
            "invalid_request": InvalidRequestError,
        }
        cls = factory.get(scenario)
        if cls is None:
            raise ValueError(f"unknown scenario {scenario!r}")
        return cls(f"{self.name}: {scenario}", provider=self.name, model=model_id)
