"""Provider layer: detect what the machine can actually serve today.

The OmniRoute-inspired "which API am I using" visibility, kept local and
honest:

  * detect_ollama_models()  — what is actually pulled, via the Ollama API
  * openrouter_key_status() — key present + well-formed (NEVER the value)
  * provider_status()       — one dict for the dashboard/CLI
  * suggest_chain()         — see bucker/models.py (deterministic config
                              suggestion; the router never reorders)

Probe pitfall (Windows): urllib in a thread hangs on localhost IPv6
resolution — the probes here use asyncio.open_connection to 127.0.0.1.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from typing import Any

from bucker.models import PROVIDERS, suggest_chain

_OLLAMA_TAGS_URL = "http://127.0.0.1:11434/api/tags"


async def _probe_http_json(url: str, timeout: float = 2.0) -> dict | None:
    """GET a JSON endpoint with a hard timeout. Pure asyncio, no urllib."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    port = parsed.port or 80
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(parsed.hostname, port),
            timeout=timeout,
        )
        try:
            writer.write(
                f"GET {parsed.path} HTTP/1.1\r\n"
                f"Host: {parsed.hostname}\r\n"
                f"Connection: close\r\n\r\n".encode()
            )
            await writer.drain()
            status_line = await asyncio.wait_for(reader.readline(), timeout=timeout)
            if b" 2" not in status_line[:12]:
                return None
            # Drain the headers before reading the body — read() would
            # otherwise return headers + body and json.loads would choke.
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=timeout)
                if line in (b"\r\n", b"\n", b""):
                    break
            body = await asyncio.wait_for(reader.read(), timeout=timeout)
            import json

            return json.loads(body)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
    except Exception:
        return None


async def detect_ollama_models(timeout: float = 2.0) -> list[str]:
    """Model names currently pulled in Ollama, sorted, or [] if unreachable."""
    data = await _probe_http_json(_OLLAMA_TAGS_URL, timeout=timeout)
    if not data or not isinstance(data, dict):
        return []
    models = data.get("models") or []
    names = sorted(m.get("name", "") for m in models if m.get("name"))
    return names


def openrouter_key_status() -> dict[str, Any]:
    """Key shape only — the value is never returned."""
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        return {"ok": False, "detail": "no key set (OPENROUTER_API_KEY)"}
    if key.startswith("sk-or-v1-") and len(key) >= 60:
        return {"ok": True, "detail": "key set, shape ok"}
    return {"ok": False, "detail": "key set, unexpected shape"}


def deepseek_key_status() -> dict[str, Any]:
    """Key shape only — the value is never returned."""
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        return {"ok": False, "detail": "no key set (DEEPSEEK_API_KEY)"}
    if key.startswith("sk-") and len(key) >= 20:
        return {"ok": True, "detail": "key set, shape ok"}
    return {"ok": False, "detail": "key set, unexpected shape"}


async def provider_status(timeout: float = 2.0) -> dict[str, Any]:
    """Everything the UI needs: providers, local models, key status."""
    ollama_models = await detect_ollama_models(timeout=timeout)
    or_key = openrouter_key_status()
    ds_key = deepseek_key_status()

    providers: dict[str, Any] = {}
    providers["ollama"] = {
        "ok": bool(ollama_models),
        "detail": f"{len(ollama_models)} model(s) pulled" if ollama_models
                  else "unreachable or nothing pulled",
        "models": ollama_models,
        "tier": PROVIDERS["ollama"]["tier"],
    }
    providers["openrouter"] = {
        "ok": or_key["ok"],
        "detail": or_key["detail"],
        "models": [],  # catalog-backed, not fetched per request
        "tier": PROVIDERS["openrouter"]["tier"],
    }
    providers["deepseek"] = {
        "ok": ds_key["ok"],
        "detail": ds_key["detail"],
        "models": [],  # catalog-backed, not fetched per request
        "tier": PROVIDERS["deepseek"]["tier"],
    }

    return {
        "providers": providers,
        "ollama_models": ollama_models,
        "openrouter_key_ok": or_key["ok"],
        "deepseek_key_ok": ds_key["ok"],
        "suggested_chain": suggest_chain(
            ollama_models=ollama_models,
            openrouter_key_ok=or_key["ok"],
            deepseek_key_ok=ds_key["ok"],
        ),
    }


def parse_model_chain(model: str, fallbacks: list[str]) -> list[dict]:
    """The configured chain, annotated with tier/provider for the UI.

    Pure — no I/O. Each entry: {id, provider, tier}.
    """
    from bucker.models import provider_of, tier_of

    out = []
    for m in [model, *fallbacks]:
        if not m:
            continue
        out.append({
            "id": m,
            "provider": provider_of(m),
            "tier": tier_of(m),
        })
    return out
