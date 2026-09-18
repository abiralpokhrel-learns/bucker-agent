"""Retry-context compaction: keep the prompt prefix stable across retries.

Why this exists (latency, not correctness):
  Prompt caching only pays off on the byte-identical prefix across calls.
  The retry loop used to do ``objective + failure_1 + failure_2 ...`` — every
  retry grew an uncached tail AND shifted the prefix, so even a perfect cache
  re-prefilled more tokens each attempt.

  Compaction keeps the ORIGINAL objective byte-identical (the cacheable
  prefix) and carries only the LATEST failure context in a bounded tail.
  Pure token-count reduction, provider-agnostic; it compounds with caching
  rather than competing with it.

  Pure logic (no I/O) so it is safe to call from Temporal workflow code and
  from the lite runner — same function, same behaviour on both stacks.
"""

from __future__ import annotations

#: Marker written by bucker.retry._build_failure_context. The first
#: occurrence separates the stable base objective from accumulated retry
#: tails. Matching is deliberately loose (substring, not exact line) so an
#: older accumulated prompt still compacts.
_FAILURE_MARKER = "failed verification"

#: Hard ceiling for a compacted retry objective. The per-attempt
#: failure_context is already bounded (~3k in bucker.retry); this bounds the
#: COMBINATION so N retries cannot grow the prompt N-fold.
MAX_RETRY_OBJECTIVE_CHARS = 4000


def base_objective(objective: str) -> str:
    """The stable prefix: everything before the first retry tail."""
    idx = objective.find(_FAILURE_MARKER)
    if idx == -1:
        return objective
    # Walk back to the start of the enclosing "Attempt N ..." block so the
    # base does not keep a dangling half-header. Fall back to a plain cut.
    head = objective[:idx]
    attempt_idx = head.rfind("Attempt ")
    if attempt_idx != -1:
        return objective[:attempt_idx].rstrip()
    return head.rstrip()


def compact_retry_objective(
    objective: str,
    failure_context: str,
    *,
    max_chars: int = MAX_RETRY_OBJECTIVE_CHARS,
) -> str:
    """Build the next-attempt objective: stable base + latest failure only.

    - Base (original objective) is preserved byte-identical when it fits, so
      a prefix cache can hit across retries.
    - Only the NEWEST failure_context is carried; older tails are dropped
      (their diagnostics already had their chance to steer a correction).
    - Total length is bounded by ``max_chars``; the failure tail is
      truncated first, never the base, unless the base alone exceeds it.
    """
    if not failure_context:
        return objective[:max_chars] if len(objective) > max_chars else objective
    base = base_objective(objective).strip()
    tail = failure_context.strip()
    if not base:
        return tail[:max_chars]
    combined = f"{base}\n\n{tail}"
    if len(combined) <= max_chars:
        return combined
    # Shrink the tail first — it is the bounded, regenerable part.
    room_for_tail = max_chars - len(base) - 2
    if room_for_tail < 200:
        # Base itself is huge: truncate base from the end is wrong (the
        # task statement matters most at the start); cut the tail hard and
        # keep the base head.
        keep_base = max_chars - 202
        return f"{base[:keep_base].rstrip()}\n\n{tail[:200]}"
    return f"{base}\n\n{tail[:room_for_tail]}"
