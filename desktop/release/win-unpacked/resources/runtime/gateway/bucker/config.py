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
    #: "docker" (default, isolated containers) or "local" (lite mode: plain
    #: host subprocesses in a scratch dir, NO isolation — trusted code only).
    sandbox_mode: str = field(
        default_factory=lambda: _env("BUCKER_SANDBOX_MODE", "docker")
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
    model: str = field(
        default_factory=lambda: _env(
            "BUCKER_MODEL", "deepseek/deepseek-v4-flash"
        )
    )
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
    #: Anthropic key (optional provider — Claude models). Never printed.
    anthropic_api_key: str = field(
        default_factory=lambda: _env("ANTHROPIC_API_KEY", "")
    )
    #: OpenAI key (optional provider — GPT models). Never printed.
    openai_api_key: str = field(
        default_factory=lambda: _env("OPENAI_API_KEY", "")
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

    # --- command verifier -------------------------------------------------
    #: Default shell command the `command` verifier runs when the task
    #: contract does not name one (constraints.command). Empty means no
    #: default — such tasks then FAIL VERIFICATION rather than pass
    #: silently, because a verifier that runs nothing must not pass.
    shell_verify_command: str = field(
        default_factory=lambda: _env("BUCKER_SHELL_VERIFY_COMMAND", "")
    )

    # --- self-critique loop (loop engineering) ------------------------------
    #: Run the critic pass (cheap model review of the diff) before verifying.
    #: Bounded to ONE repair round per attempt; disable with 0.
    enable_critique: bool = field(
        default_factory=lambda: _env("BUCKER_ENABLE_CRITIQUE", "1") == "1"
    )
    max_tokens_critic: int = field(
        default_factory=lambda: int(_env("BUCKER_MAX_TOKENS_CRITIC", "600"))
    )

    # --- memory system (harness layer) -------------------------------------
    #: Consolidate finished tasks into semantic-memory facts automatically
    #: (episodic -> semantic). Idempotent per task; disable with 0.
    auto_consolidate: bool = field(
        default_factory=lambda: _env("BUCKER_AUTO_CONSOLIDATE", "1") == "1"
    )

    # --- delivery (gateway) -------------------------------------------------
    #: Generic webhook URL for task-completion notifications. Empty = off.
    notify_webhook_url: str = field(
        default_factory=lambda: _env("BUCKER_NOTIFY_WEBHOOK_URL", "")
    )
    #: Optional shared secret for the generic webhook channel. When set,
    #: every webhook POST carries an HMAC-SHA256 signature header
    #: (X-Bucker-Signature: t=<unix>,v1=<hex>) that receivers can verify
    #: with bucker.core.notify.verify_webhook_signature. Empty = unsigned.
    notify_webhook_secret: str = field(
        default_factory=lambda: _env("BUCKER_NOTIFY_WEBHOOK_SECRET", "")
    )
    #: Slack incoming-webhook URL for task-completion notifications.
    #: Empty = off. Never printed.
    slack_webhook_url: str = field(
        default_factory=lambda: _env("BUCKER_SLACK_WEBHOOK_URL", "")
    )
    #: Discord incoming-webhook URL for task-completion notifications.
    #: Empty = off. Never printed.
    discord_webhook_url: str = field(
        default_factory=lambda: _env("BUCKER_DISCORD_WEBHOOK_URL", "")
    )
    #: Telegram delivery (requires BOTH token and chat id). Empty = off.
    telegram_bot_token: str = field(
        default_factory=lambda: _env("TELEGRAM_BOT_TOKEN", "")
    )
    telegram_chat_id: str = field(
        default_factory=lambda: _env("TELEGRAM_CHAT_ID", "")
    )

    # --- api -------------------------------------------------------------
    #: Production mode: with BUCKER_PRODUCTION=1 the API/worker refuse to
    #: boot with the dev-token default (enforced, not documented).
    production: bool = field(
        default_factory=lambda: _env("BUCKER_PRODUCTION", "0") == "1"
    )
    #: Full-access bearer token. NEVER leave as dev-token outside localhost.
    api_token: str = field(default_factory=lambda: _env("BUCKER_API_TOKEN", "dev-token"))
    #: Optional read-only token (GET routes only). Empty = no read tier;
    #: mutations always require the admin token.
    read_token: str = field(default_factory=lambda: _env("BUCKER_READ_TOKEN", ""))
    #: Writes to /memory and /skills (prompt supply chain) via the API.
    #: Turn off in production; local CLI use is unaffected.
    enable_memory_api: bool = field(
        default_factory=lambda: _env("BUCKER_ENABLE_MEMORY_API", "1") == "1"
    )

    # --- inference gateway (bucker/gateway) ----------------------------------
    # The OpenAI-compatible /v1 surface is an inference gateway now, not a
    # passthrough: it owns provider selection, fallback, retries, circuit
    # breakers, quotas, and health — Hermes/agents just make a request.
    #
    #: Default routing policy. priority = configured chain order
    #: (BUCKER_MODEL then BUCKER_MODEL_FALLBACKS), filtered through
    #: capabilities/quota/health — the old behavior, now policy-driven.
    #: Other policies: cost | latency | balanced | free_only | local_first.
    gateway_policy: str = field(
        default_factory=lambda: _env("BUCKER_GATEWAY_POLICY", "priority")
    )
    #: Hard deadline for the whole inference attempt (routing + retries +
    #: fallbacks). The engine slices this budget across attempts: a request
    #: that allows 30s total cannot spend 20s on provider A and 20s on B.
    #: Default 120s: the primary (deepseek-v4-flash) can take 20-55s alone,
    #: and the fallback reserve (see gateway_fallback_reserve_s) must still
    #: leave the chain a real slice.
    gateway_deadline_s: float = field(
        default_factory=lambda: float(_env("BUCKER_GATEWAY_DEADLINE_S", "120"))
    )
    #: Per-attempt provider timeout (connection + response). Never exceeds
    #: the remaining deadline. Default 60s so a slow-but-successful primary
    #: response (deepseek 200s observed at 20-55s) is not cut off and
    #: misclassified as unavailable.
    gateway_timeout_s: float = field(
        default_factory=lambda: float(_env("BUCKER_GATEWAY_TIMEOUT_S", "60"))
    )
    #: Retryable failures (429/5xx/timeouts) get this many retries per
    #: candidate before the engine moves to the next candidate.
    gateway_max_retries: int = field(
        default_factory=lambda: int(_env("BUCKER_GATEWAY_RETRIES", "1"))
    )
    #: Circuit breaker: consecutive failures before a provider/model is
    #: opened, and how long it stays open before a single probe is allowed.
    gateway_circuit_threshold: int = field(
        default_factory=lambda: int(_env("BUCKER_GATEWAY_CIRCUIT_THRESHOLD", "3"))
    )
    gateway_circuit_open_for_s: float = field(
        default_factory=lambda: float(
            _env("BUCKER_GATEWAY_CIRCUIT_OPEN_FOR_S", "30")
        )
    )


settings = Settings()
