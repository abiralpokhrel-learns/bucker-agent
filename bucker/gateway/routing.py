"""Routing engine (spec §7-9, §14, §16, §28, §41).

The pipeline for every request:

    InferenceRequest
      -> hard requirements (tools? streaming? vision? reasoning? context?
                            free-only? cost ceiling?)
      -> registry filter (capability metadata — spec §5)
      -> soft filters    (adapter present + key configured, circuit closed,
                          quota remaining)
      -> ranking         (policy: priority | cost | latency | balanced |
                          free_only | local_first)
      -> execution plan  (attempt candidates in order, per-attempt timeout
                          sliced from the request deadline, retries with
                          exponential backoff + jitter, fallback on failure)
      -> normalized result

Rules that keep routing predictable (spec §14, §19, §47):

  * The DEADLINE is the boss. The total budget (deadline_s) is sliced
    across attempts — a request that allows 30s cannot spend 20s on
    provider A and 20s on provider B.
  * Retries only happen for RETRYABLE failures (429/5xx/timeout), with
    backoff + jitter and bounded by the deadline. Auth errors, invalid
    requests, and model-not-found are never retried against the same
    candidate — they move to the next one (or back to the caller).
  * An InvalidRequestError is raised immediately: the request is bad
    everywhere, so routing around it would be lying.
  * Tool-call safety (spec §19): the gateway only retries INFERENCE
    failures that happened BEFORE a response was produced. A completed
    response — including one with tool calls — is returned as-is; the
    caller is the authority on tool execution. In streaming, fallback is
    only allowed before the first delta is forwarded; once content has
    reached the caller the stream cannot be switched mid-flight.
  * Impossible requests are rejected before any provider is called
    (spec §28): unknown model, model that fails hard requirements, no
    eligible candidates.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from bucker.config import settings
from bucker.gateway.adapters import ProviderAdapter, RawCompletion, default_adapters
from bucker.gateway.circuit import CircuitRegistry
from bucker.gateway.errors import (
    AllProvidersFailedError,
    GatewayError,
    GatewayTimeoutError,
    InvalidRequestError,
    NoCandidatesError,
)
from bucker.gateway.models import (
    ROUTING_POLICIES,
    InferenceRequest,
    InferenceResponse,
    stream_event,
)
from bucker.gateway.quota import QuotaManager
from bucker.gateway.registry import GatewayModel, ModelRegistry

log = logging.getLogger("bucker.gateway.engine")

#: Minimum remaining budget for one more attempt; below this we stop trying.
_MIN_ATTEMPT_S = 1.0


@dataclass(slots=True)
class RoutingDecision:
    """What the engine decided and why — the audit trail for one request."""

    request_id: str
    policy: str
    candidates: list[str]                 # canonical ids, ranked
    attempts: list[dict] = field(default_factory=list)
    selected: str | None = None
    excluded: dict[str, str] = field(default_factory=dict)  # id -> reason


class RouterEngine:
    """Policy-driven routing engine. Stateless beyond injected registries /
    adapters / circuits / quota, so multiple gateway replicas can share the
    same construction pattern (spec §34)."""

    def __init__(
        self,
        registry: ModelRegistry | None = None,
        adapters: dict[str, ProviderAdapter] | None = None,
        circuits: CircuitRegistry | None = None,
        quota: QuotaManager | None = None,
        *,
        policy: str | None = None,
        deadline_s: float | None = None,
        timeout_s: float | None = None,
        max_retries: int | None = None,
        circuit_threshold: int | None = None,
        circuit_open_for_s: float | None = None,
    ) -> None:
        self.registry = registry or ModelRegistry.default()
        self.adapters = adapters or default_adapters()
        self.circuits = circuits or CircuitRegistry(
            threshold=circuit_threshold or settings.gateway_circuit_threshold,
            open_for_s=circuit_open_for_s or settings.gateway_circuit_open_for_s,
        )
        self.quota = quota or QuotaManager()
        self.policy = policy or settings.gateway_policy
        self.deadline_s = deadline_s or settings.gateway_deadline_s
        self.timeout_s = timeout_s or settings.gateway_timeout_s
        self.max_retries = (
            settings.gateway_max_retries if max_retries is None else max_retries
        )
        if self.policy not in ROUTING_POLICIES:
            raise ValueError(
                f"unknown routing policy {self.policy!r}; "
                f"expected one of {ROUTING_POLICIES}"
            )

    # ==================================================================
    # Planning (spec §7-8, §28) — raises early on impossible requests.
    # ==================================================================
    async def plan(self, req: InferenceRequest) -> RoutingDecision:
        policy = req.policy or self.policy
        excluded: dict[str, str] = {}

        # --- 1. hard requirements -------------------------------------
        requirements = {
            "needs_tools": req.needs_tools,
            "needs_streaming": req.needs_streaming,
            "needs_vision": req.needs_vision,
            "needs_reasoning": bool(req.metadata.get("needs_reasoning")),
            "min_context": req.min_context or 0,
            "free_only": req.free_only or policy == "free_only",
            "max_cost_usd": req.max_cost_usd,
        }

        # --- 2. explicit model preference ------------------------------
        requested: GatewayModel | None = None
        if req.model:
            requested = self.registry.get(req.model)
            if requested is None:
                raise InvalidRequestError(
                    f"unknown model {req.model!r}", model=req.model
                )
            if not self._satisfies_hard(requested, requirements):
                raise InvalidRequestError(
                    f"model {req.model!r} does not satisfy the request's "
                    f"requirements (tools={requirements['needs_tools']}, "
                    f"streaming={requirements['needs_streaming']}, "
                    f"context>={requirements['min_context']}, "
                    f"free_only={requirements['free_only']})",
                    model=req.model,
                )

        # --- 3. registry filter (hard pass) -----------------------------
        candidates = self.registry.filter(**requirements)

        # --- 4. soft filters: adapter, circuit, quota --------------------
        eligible: list[GatewayModel] = []
        for model in candidates:
            adapter = self.adapters.get(model.provider)
            if adapter is None:
                excluded[model.canonical_id] = "no adapter"
                continue
            if not adapter.available():
                excluded[model.canonical_id] = "credential not configured"
                continue
            if not self.circuits.allow(model.key):
                excluded[model.canonical_id] = "circuit open"
                continue
            if not await self._quota_ok(model):
                excluded[model.canonical_id] = "quota exhausted"
                continue
            eligible.append(model)

        # --- 5. rank by policy ------------------------------------------
        ranked = self._rank(eligible, policy)
        if requested is not None and requested in ranked:
            ranked.remove(requested)
            ranked.insert(0, requested)  # explicit preference goes first
        elif requested is not None:
            log.warning(
                "requested model %s soft-excluded (%s); falling back",
                requested.canonical_id,
                excluded.get(requested.canonical_id, "unknown reason"),
            )

        if not ranked:
            reason = (
                "; ".join(f"{cid}: {why}" for cid, why in excluded.items())
                or "no models match the hard requirements"
            )
            raise NoCandidatesError(
                f"no eligible model for request (excluded: {reason})",
                safe="no eligible model for the request's requirements",
            )

        decision = RoutingDecision(
            request_id=req.request_id,
            policy=policy,
            candidates=[m.canonical_id for m in ranked],
            excluded=excluded,
        )
        log.info(
            "routing request_id=%s policy=%s ranked=%s",
            req.request_id, policy, decision.candidates,
        )
        return decision

    # ==================================================================
    # Non-streaming execution
    # ==================================================================
    async def complete(self, req: InferenceRequest) -> InferenceResponse:
        decision = await self.plan(req)
        deadline = time.monotonic() + (req.deadline_s or self.deadline_s)
        started = time.monotonic()

        for model in self._candidate_models(decision):
            if time.monotonic() + _MIN_ATTEMPT_S > deadline:
                decision.attempts.append({
                    "provider": model.provider, "model": model.canonical_id,
                    "ok": False, "error_type": "deadline_exceeded",
                })
                break
            attempt_timeout = min(
                req.timeout_s or self.timeout_s, deadline - time.monotonic()
            )
            try:
                raw = await self._attempt(model, req, attempt_timeout, deadline)
            except InvalidRequestError:
                raise  # the request is bad everywhere — never route around it
            except GatewayError as err:
                decision.attempts.append({
                    "provider": model.provider, "model": model.canonical_id,
                    "ok": False, "error_type": err.category,
                })
                self.circuits.record_failure(model.key, 0)
                log.warning(
                    "attempt failed request_id=%s provider=%s model=%s error=%s",
                    req.request_id, model.provider, model.canonical_id, err.category,
                )
                continue

            decision.selected = model.canonical_id
            latency_ms = int((time.monotonic() - started) * 1000)
            self.circuits.record_success(model.key, latency_ms)
            response = self._build_response(req, model, raw, latency_ms, decision)
            await self._record_usage(
                req, model, response.usage, "success", None,
                len(decision.attempts) + 1, latency_ms,
            )
            log.info(
                "completed request_id=%s provider=%s model=%s latency_ms=%d attempts=%d",
                req.request_id, model.provider, model.canonical_id,
                latency_ms, len(decision.attempts) + 1,
            )
            return response

        await self._record_usage(
            req, None, None, "error", "all_providers_failed_error",
            len(decision.attempts) or 1, 0,
        )
        raise AllProvidersFailedError(decision.attempts)

    # ==================================================================
    # Streaming execution (spec §17) — yields canonical events; NEVER
    # raises for provider failures (SSE headers are already sent, so the
    # caller receives an "error" event instead). Planning errors raise
    # from ``plan()`` before the stream starts.
    # ==================================================================
    async def stream(
        self, req: InferenceRequest, decision: RoutingDecision
    ) -> AsyncIterator[dict]:
        deadline = time.monotonic() + (req.deadline_s or self.deadline_s)
        started = time.monotonic()
        forwarded = False
        prompt_tokens = completion_tokens = 0

        for model in self._candidate_models(decision):
            if forwarded:
                break
            if time.monotonic() + _MIN_ATTEMPT_S > deadline:
                decision.attempts.append({
                    "provider": model.provider, "model": model.canonical_id,
                    "ok": False, "error_type": "deadline_exceeded",
                })
                break
            adapter = self.adapters[model.provider]
            try:
                async for ev in adapter.stream(req, model.provider_model_id):
                    if time.monotonic() > deadline:
                        raise GatewayTimeoutError(
                            "stream exceeded request deadline",
                            provider=model.provider, model=model.canonical_id,
                        )
                    if ev["type"] in ("text_delta", "tool_call_delta"):
                        forwarded = True
                    elif ev["type"] == "usage":
                        prompt_tokens = ev.get("prompt_tokens", 0)
                        completion_tokens = ev.get("completion_tokens", 0)
                        cost_usd = self._usage_dict(
                            model, prompt_tokens, completion_tokens
                        )["cost_usd"]
                        yield {**ev, "cost_usd": cost_usd}
                        continue
                    yield ev
            except GatewayError as err:
                decision.attempts.append({
                    "provider": model.provider, "model": model.canonical_id,
                    "ok": False, "error_type": err.category,
                })
                self.circuits.record_failure(model.key, 0)
                log.warning(
                    "stream attempt failed request_id=%s provider=%s model=%s "
                    "error=%s forwarded=%s",
                    req.request_id, model.provider, model.canonical_id,
                    err.category, forwarded,
                )
                if forwarded:
                    # Cannot switch mid-stream without corrupting the
                    # caller's partial response (spec §19): surface the
                    # error and stop.
                    await self._record_usage(
                        req, model, None, "error", err.category,
                        len(decision.attempts), 0,
                    )
                    yield stream_event("error", error_type=err.category, message=err.safe)
                    return
                continue

            # Clean stream end.
            decision.selected = model.canonical_id
            latency_ms = int((time.monotonic() - started) * 1000)
            self.circuits.record_success(model.key, latency_ms)
            usage = self._usage_dict(model, prompt_tokens, completion_tokens)
            await self._record_usage(
                req, model, usage, "success", None,
                len(decision.attempts) + 1, latency_ms,
            )
            log.info(
                "stream completed request_id=%s provider=%s model=%s latency_ms=%d",
                req.request_id, model.provider, model.canonical_id, latency_ms,
            )
            return

        await self._record_usage(
            req, None, None, "error", "all_providers_failed_error",
            len(decision.attempts) or 1, 0,
        )
        yield stream_event(
            "error", error_type="all_providers_failed_error",
            message="all providers failed",
        )

    # ==================================================================
    # Internals
    # ==================================================================
    def _candidate_models(self, decision: RoutingDecision) -> list[GatewayModel]:
        out = []
        for cid in decision.candidates:
            model = self.registry.get(cid)
            if model is not None:
                out.append(model)
        return out

    def _satisfies_hard(self, model: GatewayModel, requirements: dict) -> bool:
        if requirements["needs_tools"] and not model.supports("tools"):
            return False
        if requirements["needs_streaming"] and not model.supports("streaming"):
            return False
        if requirements["needs_vision"] and not model.supports("vision"):
            return False
        if requirements["needs_reasoning"] and not model.supports("reasoning"):
            return False
        if model.context < requirements["min_context"]:
            return False
        if requirements["free_only"] and not model.free:
            return False
        if requirements["max_cost_usd"] is not None and not model.price_unknown():
            total = model.price_input_per_m + model.price_output_per_m
            if total > requirements["max_cost_usd"]:
                return False
        return True

    async def _quota_ok(self, model: GatewayModel) -> bool:
        if model.daily_limit is None:
            return True
        remaining = await self.quota.daily_remaining(
            model.provider, model.provider_model_id, model.daily_limit
        )
        if remaining is None:
            # Ledger unreachable — fail OPEN on quota (availability first).
            # The quota module documents this trade-off.
            return True
        return remaining > 0

    def _rank(self, models: list[GatewayModel], policy: str) -> list[GatewayModel]:
        if policy == "cost":
            return sorted(models, key=lambda m: (self._cost_key(m), m.priority))
        if policy == "latency":
            return sorted(
                models,
                key=lambda m: (
                    self.circuits.avg_latency_ms(m.key)
                    if self.circuits.avg_latency_ms(m.key) is not None
                    else float("inf"),
                    m.priority,
                ),
            )
        if policy == "local_first":
            by_prio = lambda bucket: sorted(bucket, key=lambda m: m.priority)  # noqa: E731
            local = by_prio([m for m in models if m.provider == "ollama"])
            free = by_prio([m for m in models if m.free and m.provider != "ollama"])
            paid = by_prio([m for m in models if not m.free and m.provider != "ollama"])
            return local + free + paid
        if policy == "balanced":
            return sorted(models, key=self._balanced_score, reverse=True)
        return sorted(models, key=lambda m: m.priority)  # priority (default)

    @staticmethod
    def _cost_key(model: GatewayModel) -> float:
        if model.price_unknown():
            return float("inf")
        return model.price_input_per_m + model.price_output_per_m

    def _balanced_score(self, model: GatewayModel) -> float:
        """quality + cost + latency + reliability, weighted (spec §9)."""
        prio = max(0.0, 1.0 - model.priority / 100.0)
        if model.price_unknown() or model.price_input_per_m == 0:
            cost = 1.0
        else:
            cost = min(1.0, 1.0 / (1.0 + model.price_input_per_m))
        lat = self.circuits.avg_latency_ms(model.key)
        lat_score = 0.5 if lat is None else min(1.0, 2000.0 / (lat + 2000.0))
        err = self.circuits.error_rate(model.key)
        rel = 1.0 if err is None else 1.0 - err
        return 0.4 * prio + 0.2 * cost + 0.2 * lat_score + 0.2 * rel

    async def _attempt(
        self,
        model: GatewayModel,
        req: InferenceRequest,
        timeout_s: float,
        deadline: float,
    ) -> RawCompletion:
        """One candidate: up to ``1 + max_retries`` tries, retryable errors
        only, exponential backoff + jitter, bounded by the deadline."""
        adapter = self.adapters[model.provider]
        last_err: GatewayError | None = None
        max_tries = 1 + self.max_retries
        for try_i in range(max_tries):
            remaining = deadline - time.monotonic()
            if remaining <= 0.5:
                raise GatewayTimeoutError(
                    "request deadline exhausted",
                    provider=model.provider, model=model.canonical_id,
                )
            try:
                return await asyncio.wait_for(
                    adapter.complete(req, model.provider_model_id),
                    timeout=max(0.05, min(timeout_s, remaining)),
                )
            except TimeoutError:
                # wait_for fired (the adapter's own timeout may be larger):
                # same routing semantics as a provider timeout.
                last_err = GatewayTimeoutError(
                    f"{model.provider} exceeded the attempt timeout",
                    provider=model.provider, model=model.canonical_id,
                )
            except GatewayError as err:
                last_err = err
            if not last_err.retryable:
                raise last_err
            if try_i < max_tries - 1:
                backoff = 0.5 * (2 ** try_i) + random.uniform(0.0, 0.25)
                wait = min(backoff, max(0.0, deadline - time.monotonic()))
                if wait > 0:
                    await asyncio.sleep(wait)
        assert last_err is not None
        raise last_err

    @staticmethod
    def _usage_dict(
        model: GatewayModel, prompt_tokens: int, completion_tokens: int
    ) -> dict:
        cost: float | None = None
        if not model.price_unknown():
            cost = (
                prompt_tokens / 1_000_000 * model.price_input_per_m
                + completion_tokens / 1_000_000 * model.price_output_per_m
            )
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "cost_usd": cost,
        }

    def _build_response(
        self,
        req: InferenceRequest,
        model: GatewayModel,
        raw: RawCompletion,
        latency_ms: int,
        decision: RoutingDecision,
    ) -> InferenceResponse:
        return InferenceResponse(
            request_id=req.request_id,
            model=model.canonical_id,
            provider=model.provider,
            content=raw.text,
            tool_calls=raw.tool_calls,
            finish_reason=raw.finish_reason,
            usage=self._usage_dict(
                model,
                raw.usage.get("prompt_tokens", 0),
                raw.usage.get("completion_tokens", 0),
            ),
            latency_ms=latency_ms,
            attempts=len(decision.attempts) + 1,
            from_fallback=len(decision.attempts) > 0,
        )

    async def _record_usage(
        self,
        req: InferenceRequest,
        model: GatewayModel | None,
        usage_: dict | None,
        outcome: str,
        error_type: str | None,
        attempt_count: int,
        latency_ms: int,
    ) -> None:
        try:
            await self.quota.record_usage(
                request_id=req.request_id,
                tenant_id=req.tenant_id,
                purpose=req.purpose,
                provider=model.provider if model else "",
                model=model.provider_model_id if model else "",
                prompt_tokens=(usage_ or {}).get("prompt_tokens", 0),
                completion_tokens=(usage_ or {}).get("completion_tokens", 0),
                cost_usd=(usage_ or {}).get("cost_usd"),
                latency_ms=latency_ms,
                outcome=outcome,
                error_type=error_type,
                attempt_count=attempt_count,
            )
        except Exception:  # noqa: BLE001 — usage recording never breaks a request
            log.warning("usage recording failed", exc_info=True)
