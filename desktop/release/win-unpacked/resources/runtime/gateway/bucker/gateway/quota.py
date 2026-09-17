"""Quota management (spec §10-11): free access as entitlements, not booleans.

Free tiers are quotas: requests/day, tokens, promotional credits, model- or
account-specific limits. The quota manager records every gateway request in
the ``gateway_usage`` table (migration 004) and answers "how much of the
daily entitlement is left for this provider/model" so the routing engine
can stop selecting an exhausted provider until it resets.

Design rules:

  * Recording usage NEVER breaks the request — a quota insert failure is a
    data-quality gap, not a correctness bug (same rule as telemetry).
  * Quota enforcement is per provider/model against the registry's
    documented ``daily_limit``. Limits only apply when both the limit
    exists AND the ledger is reachable; without the DB the engine cannot
    know, so it fails OPEN on quota (availability first) and logs it.
  * Cost is recorded, never fabricated: unknown pricing stays NULL (the
    project's fail-closed cost rule applies to budgets, not to storage).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

log = logging.getLogger("bucker.gateway.quota")


class QuotaManager:
    """Usage ledger + daily entitlement checks, Postgres-backed."""

    def __init__(self, pool_getter: Callable[[], Any] | None = None) -> None:
        #: Lazily resolved asyncpg pool (or None => quota checks no-op).
        self._pool_getter = pool_getter

    # ------------------------------------------------------------ write --
    async def record_usage(
        self,
        *,
        request_id: str,
        tenant_id: str,
        purpose: str,
        provider: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cost_usd: float | None,
        latency_ms: int,
        outcome: str,                # "success" | "error"
        error_type: str | None = None,
        attempt_count: int = 1,
    ) -> None:
        """Append one request to the ledger. Never raises."""
        try:
            pool = self._pool()
            if pool is None:
                return
            await pool.execute(
                """
                INSERT INTO gateway_usage (
                    request_id, tenant_id, purpose, provider, model,
                    prompt_tokens, completion_tokens, total_tokens,
                    cost_usd, latency_ms, outcome, error_type, attempt_count
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                """,
                request_id,
                tenant_id,
                purpose,
                provider,
                model,
                prompt_tokens,
                completion_tokens,
                prompt_tokens + completion_tokens,
                cost_usd,
                latency_ms,
                outcome,
                error_type,
                attempt_count,
            )
        except Exception:  # noqa: BLE001 — quota must never break the call
            log.warning("quota record failed for %s/%s", provider, model, exc_info=True)

    # ------------------------------------------------------------ read --
    async def daily_remaining(self, provider: str, model: str, limit: int) -> int | None:
        """Requests left today for this provider/model, or None if unknown.

        ``limit`` is the documented daily entitlement from the registry.
        Unknown (no DB, no pool) => None, which the engine treats as "no
        quota constraint" (fail-open) — see module docstring.
        """
        try:
            pool = self._pool()
            if pool is None:
                return None
            row = await pool.fetchrow(
                """
                SELECT count(*) AS used FROM gateway_usage
                WHERE provider = $1 AND model = $2
                  AND created_at >= date_trunc('day', now())
                """,
                provider,
                model,
            )
            used = int(row["used"]) if row else 0
            return max(0, limit - used)
        except Exception:  # noqa: BLE001
            log.warning("quota check failed for %s/%s", provider, model, exc_info=True)
            return None

    # ------------------------------------------------------------- db --
    def _pool(self) -> Any | None:
        if self._pool_getter is None:
            return None
        try:
            return self._pool_getter()
        except Exception:  # noqa: BLE001
            log.warning("quota pool unavailable", exc_info=True)
            return None
