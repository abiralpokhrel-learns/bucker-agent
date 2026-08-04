"""Configuration. Everything env-driven, nothing hardcoded.

The one rule that matters here: **the model name lives in config, never in
code**. That is the mechanism behind "the LLM is the replaceable part" — when a
stronger model ships next year, swapping it is an env var, not a refactor.
CI greps for hardcoded model strings (see .github/workflows/ci.yml).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# Load .env before any Settings field resolves. Without this the file is
# decorative — every _env() call reads only the real environment, and a key
# written to .env silently never reaches the provider. Found the hard way.
#
# override=False so a real environment variable always beats the file: CI and
# production set variables directly, and a stale .env must never shadow them.
DOTENV_PATH = Path(__file__).resolve().parent.parent / ".env"

#: Why .env did not load, or None if it did. Diagnostics read this — a silent
#: failure here reproduces the exact bug this block was added to fix, so the
#: reason is recorded rather than swallowed.
DOTENV_ERROR: str | None = None
DOTENV_LOADED = False

try:
    from dotenv import load_dotenv

    if DOTENV_PATH.exists():
        # override=False so a real environment variable always wins: CI and
        # production set variables directly and must not be shadowed by a
        # stale local file.
        load_dotenv(DOTENV_PATH, override=False)
        DOTENV_LOADED = True
    else:
        DOTENV_ERROR = f"no .env file at {DOTENV_PATH}"
except ImportError:
    DOTENV_ERROR = (
        "python-dotenv is not installed, so .env is never read. Run: uv sync"
    )


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


@dataclass(frozen=True)
class Settings:
    # --- storage ---------------------------------------------------------
    database_url: str = field(
        default_factory=lambda: _env(
            "BUCKER_DATABASE_URL",
            "postgresql://bucker_app:dev@localhost:5432/bucker",
        )
    )
    blob_root: Path = field(
        default_factory=lambda: Path(_env("BUCKER_BLOB_ROOT", "./blobstore"))
    )

    # --- sandbox ---------------------------------------------------------
    #: Must contain every tool a verifier needs. Containers run with no
    #: network, so nothing can be installed at task time (see Dockerfile.sandbox).
    sandbox_image: str = field(
        default_factory=lambda: _env("BUCKER_SANDBOX_IMAGE", "bucker-sandbox:latest")
    )

    # --- temporal --------------------------------------------------------
    temporal_host: str = field(
        default_factory=lambda: _env("BUCKER_TEMPORAL_HOST", "localhost:7233")
    )
    temporal_namespace: str = field(
        default_factory=lambda: _env("BUCKER_TEMPORAL_NAMESPACE", "default")
    )
    task_queue: str = field(
        default_factory=lambda: _env("BUCKER_TASK_QUEUE", "bucker-tasks")
    )

    # --- model router (step 14) -----------------------------------------
    #: Never hardcode a model name anywhere else in the codebase.
    model: str = field(default_factory=lambda: _env("BUCKER_MODEL", "gpt-4o-mini"))
    #: Comma-separated fallback chain tried in order when the primary fails
    #: (provider down, key rejected, quota exhausted). Same spirit as a
    #: gateway's auto-fallback: a dead provider should not take down a task.
    #: Recorded-mode replay stays keyed to the PRIMARY model, so determinism
    #: is unaffected by the chain.
    model_fallbacks: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            m.strip()
            for m in _env("BUCKER_MODEL_FALLBACKS", "").split(",")
            if m.strip()
        )
    )

    # --- provider keys (values never logged; litellm reads the same env) ----
    #: OpenRouter key (free/paid hosted models). Never printed.
    openrouter_api_key: str = field(
        default_factory=lambda: _env("OPENROUTER_API_KEY", "")
    )
    #: DeepSeek key (paid hosted models). Never printed.
    deepseek_api_key: str = field(
        default_factory=lambda: _env("DEEPSEEK_API_KEY", "")
    )
    #: DeepSeek OpenAI-compatible endpoint (default is official).
    deepseek_base_url: str = field(
        default_factory=lambda: _env(
            "DEEPSEEK_BASE_URL", "https://api.deepseek.com"
        )
    )
    #: "live" hits the provider; "recorded" replays stored blobs (step 15) and
    #: is the default so tests and iteration cost nothing.
    model_mode: str = field(default_factory=lambda: _env("BUCKER_MODEL_MODE", "recorded"))

    # --- output ceilings -------------------------------------------------
    # ALWAYS send max_tokens. Omitting it lets the provider assume the model's
    # maximum — 64k on current frontier models — and providers that reserve
    # credits up front (OpenRouter) will refuse the request outright on a small
    # balance, even though the real answer is a few hundred tokens.
    #
    # It is also a cost ceiling in its own right, which is the whole point of
    # this project: an unbounded generation is an unbounded bill. Sized to the
    # actual job rather than to what the model permits.
    max_tokens_planner: int = field(
        default_factory=lambda: int(_env("BUCKER_MAX_TOKENS_PLANNER", "2000"))
    )
    max_tokens_worker: int = field(
        default_factory=lambda: int(_env("BUCKER_MAX_TOKENS_WORKER", "8000"))
    )
    max_tokens_default: int = field(
        default_factory=lambda: int(_env("BUCKER_MAX_TOKENS", "4000"))
    )

    # --- guardrails (step 32) -------------------------------------------
    default_budget_usd: float = field(
        default_factory=lambda: float(_env("BUCKER_DEFAULT_BUDGET_USD", "0.75"))
    )
    default_deadline_minutes: int = field(
        default_factory=lambda: int(_env("BUCKER_DEFAULT_DEADLINE_MINUTES", "15"))
    )
    max_retries: int = field(
        default_factory=lambda: int(_env("BUCKER_MAX_RETRIES", "2"))
    )

    # --- api -------------------------------------------------------------
    api_token: str = field(default_factory=lambda: _env("BUCKER_API_TOKEN", "dev-token"))


settings = Settings()
