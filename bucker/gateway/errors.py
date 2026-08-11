"""Normalized error taxonomy for the inference gateway.

Every provider failure crosses this boundary exactly once: the provider
adapters translate provider-specific errors (status codes, provider error
bodies, transport failures) into one of these categories, and everything
downstream — the routing engine, the API, the operator — reasons about the
category, never about a provider's JSON shape.

The taxonomy is deliberately small and routing-relevant (spec §46-47):

  * AuthenticationError      — key rejected. Do NOT retry the same
                               credential; move to the next provider.
  * RateLimitError           — provider is rate-limiting us. Retryable with
                               backoff, but also a signal to prefer another
                               candidate for a while.
  * QuotaExceededError       — the provider's quota/entitlement is spent.
                               Stop selecting this provider until reset.
  * ModelUnavailableError    — model not found / disabled / not served.
                               Remove this model from the candidate list.
  * ProviderUnavailableError — provider 5xx / DNS / connection reset.
                               Retryable; also feeds the circuit breaker.
  * GatewayTimeoutError      — our timeout or the provider's. Retryable
                               only while the request deadline remains.
  * ContextLengthError       — request exceeds the model's context window.
                               Do NOT retry the same model; a larger-context
                               candidate may still work.
  * InvalidRequestError      — the REQUEST is bad (schema, unsupported
                               option). Never retried, never routed around:
                               it will fail on every provider.
  * InternalGatewayError     — a bug in the gateway itself.

Each error carries ``provider`` / ``model`` where known and a ``safe``
message that may be shown to API callers: it never embeds provider response
bodies, credentials, or internal infrastructure details (spec §45-46).
"""

from __future__ import annotations


class GatewayError(Exception):
    """Base class. ``safe`` is what the API may return to callers."""

    category = "gateway_error"
    status_code = 500
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        safe: str | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.model = model
        # The public-facing message. Defaults to the category name so an
        # internal detail can never leak by forgetting to pass ``safe``.
        self.safe = safe or self.category

    def __str__(self) -> str:  # internal/debug view keeps the full message
        return super().__str__()


class AuthenticationError(GatewayError):
    category = "authentication_error"
    status_code = 401


class RateLimitError(GatewayError):
    category = "rate_limit_error"
    status_code = 429
    retryable = True


class QuotaExceededError(GatewayError):
    category = "quota_exceeded_error"
    status_code = 429


class ModelUnavailableError(GatewayError):
    category = "model_unavailable_error"
    status_code = 404


class ProviderUnavailableError(GatewayError):
    category = "provider_unavailable_error"
    status_code = 503
    retryable = True


class GatewayTimeoutError(GatewayError):
    category = "timeout_error"
    status_code = 504
    retryable = True


class EmptyCompletionError(GatewayError):
    """Provider returned HTTP 200 with no content and no tool call.

    Reasoning models (DeepSeek v4-flash) can spend the whole output
    budget on ``reasoning_content`` and return an empty message. The
    response is well-formed — the model is NOT down — so retrying the
    SAME model with the SAME prompt and budget reproduces the same empty
    result and only burns the deadline. Not retryable per-candidate:
    the engine moves straight to the next candidate, preserving the
    request deadline for the fallback chain.
    """

    category = "empty_response"
    status_code = 502


class ContextLengthError(GatewayError):
    category = "context_length_error"
    status_code = 400


class InvalidRequestError(GatewayError):
    category = "invalid_request_error"
    status_code = 400


class InternalGatewayError(GatewayError):
    category = "internal_gateway_error"
    status_code = 500


class NoCandidatesError(GatewayError):
    """Nothing in the registry satisfied the request's hard requirements.

    Raised BEFORE any provider is called (spec §28: reject impossible
    routing requests early rather than discovering the problem after
    several provider attempts).
    """

    category = "no_candidates_error"
    status_code = 503


class AllProvidersFailedError(GatewayError):
    """Every eligible candidate was attempted and failed.

    ``attempts`` carries the per-candidate outcome (provider/model/category)
    for observability; the safe message stays generic.
    """

    category = "all_providers_failed_error"
    status_code = 503
    retryable = True

    def __init__(self, attempts: list[dict]) -> None:
        self.attempts = attempts
        tried = ", ".join(
            f"{a.get('provider', '?')}/{a.get('model', '?')}:"
            f"{a.get('error_type', '?')}"
            for a in attempts
        )
        super().__init__(
            f"all {len(attempts)} candidate(s) failed: {tried}",
            safe=f"all providers failed ({len(attempts)} attempted)",
        )
