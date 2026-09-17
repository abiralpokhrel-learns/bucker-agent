"""Hermetic desktop gateway tests: real engine/adapters, mocked HTTP only."""

import importlib
import json

import pytest
from fastapi.testclient import TestClient

TOKEN = "test-ephemeral-token-not-a-real-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def connection(provider="groq", model="test-model", **changes):
    endpoints = {
        "groq": "https://api.groq.com/openai/v1",
        "openrouter": "https://openrouter.ai/api/v1",
        "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
        "sambanova": "https://api.sambanova.ai/v1",
        "mistral": "https://api.mistral.ai/v1",
        "huggingface": "https://router.huggingface.co/v1",
    }
    return dict(
        provider=provider,
        model=model,
        base_url=endpoints[provider],
        api_key="test-provider-secret",
        context=32000,
        max_output=4000,
        free=True,
        tools=True,
        **changes,
    )


def gateway():
    try:
        return importlib.import_module("bucker.desktop_gateway")
    except ModuleNotFoundError:
        pytest.fail("isolated desktop gateway package is not implemented")


def test_auth_and_configured_only_models():
    app = gateway().create_app(token=TOKEN, connections=[connection()])
    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200
        for path in ["/health/ready", "/v1/models", "/usage", "/docs", "/unknown"]:
            response = client.get(path)
            assert response.status_code == 401
            assert response.json()["error"]["type"] == "authentication_error"
        assert client.get("/health/ready", headers=AUTH).json()["status"] == "ready"
        response = client.get("/v1/models", headers=AUTH)
        assert response.status_code == 200
        assert [item["id"] for item in response.json()["data"]] == [
            "desktop-auto",
            "groq/test-model",
        ]
        assert "test-provider-secret" not in response.text
        assert client.get("/docs", headers=AUTH).status_code == 404
        assert (
            client.get("/health/ready", headers={"Authorization": "Bearer wrong"}).status_code
            == 401
        )
    assert [m.canonical_id for m in app.state.engine.registry.all()] == ["groq/test-model"]
    assert app.state.engine.policy == "free_only"


def test_invalid_configuration_fails_closed():
    module = gateway()
    bad = [
        [],
        [dict(connection(), base_url="http://127.0.0.1:1234/v1")],
        [dict(connection(), free=False)],
        [dict(connection(), provider="openai")],
        [connection("openrouter", "paid-model")],
        [dict(connection(), api_key="")],
        [dict(connection(), context=True)],
        [dict(connection(), extra="ignored")],
    ]
    for connections in bad:
        with pytest.raises(ValueError):
            module.create_app(token=TOKEN, connections=connections)
    with pytest.raises(ValueError):
        module.create_app(token="", connections=[connection()])
    for provider in ["openrouter", "gemini", "groq", "sambanova", "mistral", "huggingface"]:
        model = "model:free" if provider == "openrouter" else "model"
        module.create_app(token=TOKEN, connections=[connection(provider, model)])
    # Multiple models may share a provider, but not conflicting credentials.
    with pytest.raises(ValueError):
        module.create_app(
            token=TOKEN,
            connections=[connection(), dict(connection(model="other"), api_key="different")],
        )
    with pytest.raises(ValueError):
        module.create_app(token=TOKEN, connections=[connection(), connection()])


def test_import_disables_dotenv_before_core_import(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    # Instrument file access rather than modifying the real project's .env.
    script = """
import builtins, os, sys
original = builtins.open
def guard(file, *args, **kwargs):
    if str(file).endswith('.env'):
        raise AssertionError('attempted dotenv read')
    return original(file, *args, **kwargs)
builtins.open = guard
import bucker.desktop_gateway
assert os.environ['PYTHON_DOTENV_DISABLED'] == '1'
assert 'bucker.api.app' not in sys.modules
assert 'bucker.api.gateway' not in sys.modules
print('isolated')
"""
    env = dict(os.environ, PYTHON_DOTENV_DISABLED="0")
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "isolated"


def mock_provider(app, handler):
    import httpx

    for adapter in app.state.engine.adapters.values():
        # Exercise the real adapter translation and engine, replace only HTTP.
        adapter._client = httpx.AsyncClient(
            base_url=adapter.base_url,
            transport=httpx.MockTransport(handler),
            headers={"Authorization": f"Bearer {adapter.api_key}"},
        )


def test_completion_tools_and_429_fallback_are_recorded():
    import httpx

    calls = []
    tool_call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "lookup", "arguments": '{"q":"hi"}'},
    }
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None, "tool_calls": [tool_call]},
        {"role": "tool", "tool_call_id": "call_1", "content": "found"},
    ]
    tools = [
        {"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}
    ]

    def handler(request):
        payload = json.loads(request.content)
        calls.append((str(request.url), payload))
        assert request.headers["authorization"] == "Bearer test-provider-secret"
        assert payload["messages"] == messages
        assert payload["tools"] == tools
        assert payload["tool_choice"] == "auto"
        if request.url.host == "api.groq.com":
            return httpx.Response(
                429, json={"error": {"message": "secret raw provider details"}}
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": None, "tool_calls": [tool_call]},
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3},
            },
        )

    app = gateway().create_app(
        token=TOKEN, connections=[connection(), connection("gemini", "second")]
    )
    mock_provider(app, handler)
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=AUTH,
            json={
                "model": "desktop-auto",
                "messages": messages,
                "tools": tools,
                "tool_choice": "auto",
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["model"] == "desktop-auto"
        assert response.json()["choices"][0]["message"]["tool_calls"] == [tool_call]
        assert response.headers["x-served-model"] == "gemini/second"
        assert response.json()["usage"]["total_tokens"] == 10
        ledger = client.get("/usage", headers=AUTH).json()
        assert ledger["scope"] == "session"
        assert ledger["remaining_quota"] is None
        assert ledger["cost_usd"] is None
        entry = ledger["recent_requests"][-1]
        assert entry["provider"] == "gemini"
        assert entry["served_model"] == "gemini/second"
        assert entry["attempt_count"] == 2
        assert entry["fallback_attempts"][0]["error_type"] == "rate_limit_error"
        assert entry["usage"]["total_tokens"] == 10
        assert "test-provider-secret" not in json.dumps(ledger)
    assert [url for url, _ in calls] == [
        "https://api.groq.com/openai/v1/chat/completions",
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
    ]
    assert all(adapter._client.is_closed for adapter in app.state.engine.adapters.values())


@pytest.mark.parametrize(
    "extra",
    [
        {"free_only": False},
        {"n": 2},
        {"parallel_tool_calls": True},
        {"stop": ["END"]},
        {"messages": []},
        {"messages": [{"role": "invalid", "content": "test-provider-secret"}]},
    ],
)
def test_unsupported_requests_rejected_without_echo(extra):
    app = gateway().create_app(token=TOKEN, connections=[connection()])
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=AUTH,
            json={
                "model": "desktop-auto",
                "messages": [{"role": "user", "content": "hi"}],
                **extra,
            },
        )
        assert response.status_code == 422
        assert response.json()["error"]["type"] == "invalid_request_error"
        assert "test-provider-secret" not in response.text


def test_unknown_models_and_no_tool_candidates_rejected_before_provider():
    app = gateway().create_app(token=TOKEN, connections=[dict(connection(), tools=False)])

    def handler(request):
        pytest.fail("invalid requests must not reach a provider")

    mock_provider(app, handler)
    with TestClient(app) as client:
        for payload, status in [
            ({"model": "unconfigured"}, 400),
            ({"tools": [{"type": "function", "function": {"name": "f"}}]}, 503),
        ]:
            response = client.post(
                "/v1/chat/completions",
                headers=AUTH,
                json={
                    "messages": [{"role": "user", "content": "hi"}],
                    **payload,
                },
            )
            assert response.status_code == status, response.text
            assert "error" in response.json()


def test_provider_errors_are_structured_and_redacted():
    import httpx

    app = gateway().create_app(token=TOKEN, connections=[connection()])
    mock_provider(
        app,
        lambda _: httpx.Response(
            400, json={"error": {"message": "test-provider-secret raw upstream"}}
        ),
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=AUTH,
            json={
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code == 400
        assert response.json()["error"]["type"] == "invalid_request_error"
        assert "secret" not in response.text
        assert "upstream" not in response.text
        assert client.get("/usage", headers=AUTH).json()["errors"] == 1


def sse_frame(delta=None, finish=None, usage=None):
    payload = {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
    if usage is not None:
        payload["usage"] = usage
    return ("data: " + json.dumps(payload) + "\n\n").encode()


@pytest.mark.parametrize("interrupt", [False, True])
def test_stream_tools_preserved_and_no_fallback_after_delta(interrupt):
    import httpx

    calls = []

    class ProviderStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield sse_frame(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": '{"q":'},
                        }
                    ]
                }
            )
            if interrupt:
                raise httpx.ReadError("raw secret provider disconnect")
            yield sse_frame({"tool_calls": [{"index": 0, "function": {"arguments": '"hi"}'}}]})
            yield sse_frame(
                finish="tool_calls", usage={"prompt_tokens": 7, "completion_tokens": 4}
            )
            yield b"data: [DONE]\n\n"

    def handler(request):
        calls.append(request.url.host)
        payload = json.loads(request.content)
        assert payload["stream"] is True
        assert payload["stream_options"] == {"include_usage": True}
        return httpx.Response(200, stream=ProviderStream())

    app = gateway().create_app(
        token=TOKEN, connections=[connection(), connection("gemini", "second")]
    )
    mock_provider(app, handler)
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=AUTH,
            json={
                "model": "desktop-auto",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
                "stream_options": {"include_usage": True},
                "tools": [{"type": "function", "function": {"name": "lookup"}}],
            },
        )
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.text.endswith("data: [DONE]\n\n")
        frames = [
            json.loads(line[6:])
            for line in response.text.splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"
        ]
        chunks = [f for f in frames if "choices" in f]
        assert all(f["model"] == "desktop-auto" for f in chunks)
        deltas = [
            f["choices"][0]["delta"]["tool_calls"][0]
            for f in chunks
            if f["choices"] and "tool_calls" in f["choices"][0]["delta"]
        ]
        assert deltas[0]["id"] == "call_1"
        assert deltas[0]["type"] == "function"
        assert deltas[0]["function"]["name"] == "lookup"
        if interrupt:
            assert frames[-1]["error"]["type"] == "provider_unavailable_error"
            assert "raw secret" not in response.text
        else:
            assert "id" not in deltas[1]  # SDK concatenates repeated ids/names!
            assert "name" not in deltas[1]["function"]
            assert "".join(d["function"]["arguments"] for d in deltas) == '{"q":"hi"}'
            assert chunks[-1]["usage"]["total_tokens"] == 11
        ledger = client.get("/usage", headers=AUTH).json()
        assert ledger["requests"] == 1
        assert ledger["errors"] == int(interrupt)
        record = ledger["recent_requests"][-1]
        assert record["served_model"] == "groq/test-model"
        assert record["provider"] == "groq"
        assert record["attempt_count"] == 1
    assert calls == ["api.groq.com"]


def test_stream_fallback_before_output_and_planning_errors():
    import httpx

    calls = []

    def handler(request):
        calls.append(request.url.host)
        if request.url.host == "api.groq.com":
            return httpx.Response(429, json={"error": {"message": "rate limited"}})
        return httpx.Response(
            200,
            content=sse_frame({"content": "hello"})
            + sse_frame(finish="stop")
            + b"data: [DONE]\n\n",
        )

    app = gateway().create_app(
        token=TOKEN, connections=[connection(), connection("gemini", "second")]
    )
    mock_provider(app, handler)
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=AUTH,
            json={
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
        assert response.status_code == 200
        assert '"content": "hello"' in response.text
        record = client.get("/usage", headers=AUTH).json()["recent_requests"][-1]
        assert record["served_model"] == "gemini/second"
        assert record["attempt_count"] == 2
        assert record["usage"] is None  # unreported != zero tokens
        assert record["fallback_attempts"][0]["error_type"] == "rate_limit_error"
        invalid = client.post(
            "/v1/chat/completions",
            headers=AUTH,
            json={
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
                "model": "unknown",
            },
        )
        assert invalid.status_code == 400
        assert invalid.headers["content-type"] == "application/json"
    assert calls == ["api.groq.com", "generativelanguage.googleapis.com"]


def test_cli_starts_real_loopback_server_and_reports_ready():
    import os
    import queue
    import subprocess
    import sys
    import threading
    from pathlib import Path

    import httpx

    env = dict(
        os.environ,
        BUCKER_DESKTOP_TOKEN=TOKEN,
        BUCKER_DESKTOP_CONNECTIONS=json.dumps([connection()]),
        PYTHON_DOTENV_DISABLED="0",
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "bucker.desktop_gateway", "--port", "0"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    lines = queue.Queue()
    threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True).start()
    try:
        ready_line = lines.get(timeout=20)
        assert ready_line, (
            process.stderr.read() if process.poll() is not None else "no ready output"
        )
        ready = json.loads(ready_line)
        assert ready["status"] == "ready"
        assert ready["host"] == "127.0.0.1"
        # Windows venv python.exe may redirect to a different serving PID.
        assert isinstance(ready["pid"], int) and ready["pid"] > 0
        assert ready["port"] > 0
        with httpx.Client(
            base_url=f"http://127.0.0.1:{ready['port']}", trust_env=False
        ) as client:
            assert client.get("/health/live").status_code == 200
            assert client.get("/health/ready").status_code == 401
            assert client.get("/health/ready", headers=AUTH).json()["status"] == "ready"
    finally:
        process.terminate()
        process.communicate(timeout=10)


def test_missing_nonstream_usage_is_unknown_not_zero():
    import httpx

    app = gateway().create_app(token=TOKEN, connections=[connection()])
    mock_provider(
        app,
        lambda _: httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
            },
        ),
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=AUTH,
            json={
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code == 200
        assert response.json()["usage"] is None
        assert client.get("/usage", headers=AUTH).json()["recent_requests"][-1]["usage"] is None


def test_malformed_upstream_is_normalized_and_falls_back():
    import httpx

    app = gateway().create_app(
        token=TOKEN, connections=[connection(), connection("gemini", "second")]
    )

    def handler(request):
        if request.url.host == "api.groq.com":
            return httpx.Response(200, json={"choices": [{"message": "secret-invalid-shape"}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "hello"}}]})

    mock_provider(app, handler)
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=AUTH,
            json={
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code == 200
        assert response.headers["x-served-model"] == "gemini/second"
        assert "secret-invalid" not in response.text


def test_cli_invalid_configuration_reports_safe_json():
    import os
    import subprocess
    import sys
    from pathlib import Path

    env = dict(
        os.environ,
        BUCKER_DESKTOP_TOKEN=TOKEN,
        BUCKER_DESKTOP_CONNECTIONS='[{"api_key":"secret-must-not-leak"}]',
    )
    result = subprocess.run(
        [sys.executable, "-m", "bucker.desktop_gateway", "--port", "0"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode != 0
    assert json.loads(result.stdout)["status"] == "error"
    assert "secret-must-not-leak" not in result.stdout + result.stderr
