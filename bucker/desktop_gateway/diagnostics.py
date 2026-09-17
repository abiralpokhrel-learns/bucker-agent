"""Public error hints from a closed vocabulary; never upstream messages."""
from bucker.gateway.errors import GatewayError

_HINTS = {
    "authentication_error": (401, "Provider rejected the API key. Replace it in Settings → Providers."),
    "rate_limit_error": (429, "Provider rate limit reached. Wait or select another free model in Settings → Providers."),
    "quota_exceeded_error": (429, "Provider free quota is exhausted. Wait for its reset or connect another free provider in Settings."),
    "model_unavailable_error": (404, "Model is unavailable. Select another model in Settings → Providers."),
    "context_length_error": (400, "Conversation exceeds the model context. Start a new session or select a larger-context model."),
    "invalid_request_error": (400, "Provider rejected the request format or options. Check model tool compatibility in Settings."),
    "timeout_error": (504, "Provider timed out. Check connectivity or select another free model in Settings."),
    "deadline_exceeded": (504, "Provider request deadline exceeded. Select another free model in Settings."),
    "provider_unavailable_error": (503, "Provider connection failed or its response was incomplete. Check connectivity or select another free model in Settings."),
    "empty_response": (502, "Model returned no answer or tool call. Select another tool-capable model in Settings."),
    "no_candidates_error": (503, "No eligible model is ready. Check connected models in Settings or wait for cooldown."),
    "all_providers_failed_error": (503, "All connected models failed. Check provider keys, free quotas and availability in Settings."),
}


def public_error(category, attempts=()):
    if category == "all_providers_failed_error" and attempts:
        reasons = {a.get("error_type") for a in attempts}
        if len(reasons) == 1:
            category = reasons.pop()
    status, hint = _HINTS.get(category, (500, "Bucker inference gateway failed. Reconnect the agent; check Settings if this continues."))
    error = GatewayError("desktop inference failed", safe=hint)
    error.category = category if category in _HINTS else "gateway_error"
    error.status_code = status
    return error
