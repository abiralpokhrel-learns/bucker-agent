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
    #: "live" hits the provider; "recorded" replays stored blobs (step 15) and
    #: is the default so tests and iteration cost nothing.
    model_mode: str = field(default_factory=lambda: _env("BUCKER_MODEL_MODE", "recorded"))

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
