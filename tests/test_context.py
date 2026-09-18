"""Context-compaction tests: stable prefix + bounded tail across retries."""

from bucker.core.context import (
    MAX_RETRY_OBJECTIVE_CHARS,
    base_objective,
    compact_retry_objective,
)


def test_base_objective_without_tail_is_unchanged():
    assert base_objective("Fix the bug") == "Fix the bug"


def test_compact_keeps_base_byte_identical():
    base = "Fix the login bug"
    first = compact_retry_objective(base, "Attempt 1 failed verification.\nBad.")
    assert first.startswith(base)
    second = compact_retry_objective(first, "Attempt 2 failed verification.\nWorse.")
    # Base prefix stable -> cacheable; only the latest failure is carried.
    assert second.startswith(base)
    assert "Attempt 1" not in second
    assert "Attempt 2" in second


def test_compact_is_bounded():
    base = "Do the thing"
    big = "Attempt 9 failed verification.\n" + ("x" * 50_000)
    out = compact_retry_objective(base, big)
    assert len(out) <= MAX_RETRY_OBJECTIVE_CHARS
    assert out.startswith(base)


def test_empty_failure_returns_objective():
    assert compact_retry_objective("Base", "") == "Base"


def test_adaptive_default_uses_compaction():
    import asyncio

    from bucker.activities.pipeline import choose_adaptive_strategy

    acc = "fix the bug\n\nAttempt 1 failed verification.\nOld news."
    out = asyncio.run(choose_adaptive_strategy({
        "attempt": 1,
        "objective": acc,
        "failure_context": "Attempt 2 failed verification.\nNew news.",
        "diagnostics": ["boom"],
        "passed": [False],
        "models_used": [""],
        "current_model": None,
    }))
    assert out["strategy"] == "default"
    assert out["next_objective"].startswith("fix the bug")
    assert "Old news" not in out["next_objective"]
    assert "New news" in out["next_objective"]
