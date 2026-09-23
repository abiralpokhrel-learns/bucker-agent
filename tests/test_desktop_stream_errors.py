"""Regression coverage for actionable, sanitized desktop streaming failures."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.test_desktop_gateway import AUTH, TOKEN, connection, gateway, mock_provider


@pytest.mark.parametrize("stream", [True, False])
@pytest.mark.parametrize("status,category", [
    (401, "authentication_error"), (429, "rate_limit_error"),
])
def test_rejected_key_is_not_reported_as_stream_interruption(status, category, stream):
    app = gateway().create_app(token=TOKEN, connections=[connection()])
    mock_provider(app, lambda _: httpx.Response(
        status, json={"error": {"message": "private-key-and-provider-body"}}
    ))
    with TestClient(app) as client:
        response = client.post("/v1/chat/completions", headers=AUTH, json={
            "messages": [{"role": "user", "content": "hello"}], "stream": stream,
        })
        if stream:
            frames = [json.loads(line[6:]) for line in response.text.splitlines()
                      if line.startswith("data: ") and line != "data: [DONE]"]
            error = next(frame["error"] for frame in frames if "error" in frame)
        else:
            error = response.json()["error"]
            assert response.status_code == status
        assert error["type"] == category
        assert error["code"] == status
        assert "Settings" in error["message"]
        assert "private-key-and-provider-body" not in response.text


@pytest.mark.parametrize("body", [
    b'data: {"error":{"code":429,"message":"private-key-and-provider-body"}}\n\n',
    b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n',
])
def test_inline_error_and_truncated_stream_are_not_success(body):
    app = gateway().create_app(token=TOKEN, connections=[connection()])
    mock_provider(app, lambda _: httpx.Response(200, content=body))
    with TestClient(app) as client:
        response = client.post("/v1/chat/completions", headers=AUTH, json={
            "messages": [{"role": "user", "content": "hello"}], "stream": True,
        })
        frames = [json.loads(line[6:]) for line in response.text.splitlines()
                  if line.startswith("data: ") and line != "data: [DONE]"]
        error = next((frame["error"] for frame in frames if "error" in frame), None)
        assert error is not None
        expected = "rate_limit_error" if b'"error"' in body else "provider_unavailable_error"
        assert error["type"] == expected
        assert "private-key-and-provider-body" not in response.text
        assert not any(c.get("finish_reason") for f in frames for c in f.get("choices", []))
        assert client.get("/usage", headers=AUTH).json()["errors"] == 1

def test_gateway_deadline_allows_long_free_generations():
    app = gateway().create_app(token=TOKEN, connections=[connection()])
    engine = app.state.engine
    assert engine.deadline_s == 300, f"deadline {engine.deadline_s}s kills slow free-model streams"
    assert engine.timeout_s == 120, f"per-attempt timeout {engine.timeout_s}s too tight for reasoning models"


def test_ttft_budget_tolerates_free_tier_queueing():
    """Free tiers queue 30-60s before the first token; a 20s TTFT budget
    abandoned healthy candidates and surfaced as 'request timed out'."""
    app = gateway().create_app(token=TOKEN, connections=[connection()])
    engine = app.state.engine
    assert engine.ttft_timeout_s >= 45, (
        f"TTFT budget {engine.ttft_timeout_s}s abandons queued free-tier candidates"
    )


def test_adapter_client_timeout_matches_engine_budget():
    """The httpx client must not abort before the engine's attempt budget:
    a 60s read timeout used to kill non-streaming generations the engine
    would have allowed to run to 120s."""
    app = gateway().create_app(token=TOKEN, connections=[connection()])
    for adapter in app.state.engine.adapters.values():
        timeout = adapter._client_for().timeout
        assert timeout.read >= 120, (
            f"read timeout {timeout.read}s undercuts the engine's 120s attempt budget"
        )


def test_transient_provider_failure_is_retried_once():
    """A single 429 must get a second attempt instead of failing the request."""
    calls = {"n": 0}

    def handler(_):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"error": {"message": "slow down"}})
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        })

    app = gateway().create_app(token=TOKEN, connections=[connection()])
    mock_provider(app, handler)
    with TestClient(app) as client:
        response = client.post("/v1/chat/completions", headers=AUTH, json={
            "messages": [{"role": "user", "content": "hello"}],
        })
    assert response.status_code == 200, response.text
    assert calls["n"] == 2, "expected exactly one retry after the 429"
