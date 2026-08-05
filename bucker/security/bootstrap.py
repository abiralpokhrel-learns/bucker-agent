"""Boot-time security enforcement (hardening review).

"Please configure this safely" comments are not enforcement. This module
is the enforcement: processes refuse to boot into configurations that are
only acceptable for local development.

Current rule:
- BUCKER_PRODUCTION=1 requires a real BUCKER_API_TOKEN. With the
  dev-token default the API and worker exit with a clear error instead of
  serving an unauthenticated control plane.

Call from the API entrypoint and the worker entrypoint.
"""

from __future__ import annotations

import sys


def assert_safe_boot(*, component: str) -> None:
    """Exit the process when the configuration is unsafe for its mode."""
    from bucker.config import settings

    if settings.production and settings.api_token == "dev-token":
        print(
            f"[bucker:{component}] refusing to boot in production mode with "
            "the dev-token default.\n"
            "  BUCKER_PRODUCTION=1 requires a real BUCKER_API_TOKEN.\n"
            "  Set a strong token (e.g. `openssl rand -hex 32`) in .env, or "
            "unset BUCKER_PRODUCTION for local development.",
            file=sys.stderr,
        )
        sys.exit(2)
