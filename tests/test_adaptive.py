"""Adaptive planning tests (step 34).

Pure logic — no database or model calls needed.
"""

from __future__ import annotations

import asyncio

from bucker.adaptive import (
    AttemptHistory,
    Strategy,
    choose_strategy,
    chunk_objective,
    clarify_objective,
    next_model,
)

# ------------------------------------------------------------ strategy ----


def test_first_failure_default():
    h = AttemptHistory(attempt=1, verifier_name="python_test_runner",
                       diagnostics=["ImportError"], passed=[False])
    assert choose_strategy(h) == Strategy.DEFAULT


def test_second_failure_switch_model():
    h = AttemptHistory(attempt=2, verifier_name="python_test_runner",
                       diagnostics=["ImportError", "ImportError"],
                       passed=[False, False],
                       models_used=["gpt-4", "gpt-4"])
    assert choose_strategy(h) == Strategy.SWITCH_MODEL


def test_second_failure_already_switched_chunk():
    """Already tried two models — try chunking."""
    h = AttemptHistory(attempt=2, verifier_name="python_test_runner",
                       diagnostics=["ImportError", "ImportError"],
                       passed=[False, False],
                       models_used=["gpt-4", "claude"])
    assert choose_strategy(h) == Strategy.CHUNK


def test_third_failure_clarify():
    h = AttemptHistory(attempt=3, verifier_name="python_test_runner",
                       diagnostics=["x", "y", "z"],
                       passed=[False, False, False])
    assert choose_strategy(h) == Strategy.CLARIFY


def test_no_failures_default():
    h = AttemptHistory(attempt=0, passed=[])
    assert choose_strategy(h) == Strategy.DEFAULT


# ----------------------------------------------------- plan modifiers ----


def test_chunk_objective_adds_structure():
    result = chunk_objective("Fix the bug", ["ImportError: no module 'foo'"])
    assert "Fix the bug" in result
    assert "ImportError" in result
    assert "smaller steps" in result.lower()


def test_clarify_objective_asks_question():
    result = clarify_objective("Add subtract function",
                               ["test failed", "import error"])
    assert "Add subtract function" in result
    assert "clarifying question" in result.lower()


# ------------------------------------------------------- model switching ----


def test_next_model_picks_unused():
    result = next_model("gpt-4", ["gpt-4"])
    assert result != "gpt-4"


def test_next_model_skips_already_tried():
    result = next_model("gpt-4", ["gpt-4"])
    assert result != "gpt-4"
    # Should be the first fallback not in the used list.
    assert "nvidia" in result or "minimax" in result or "claude" in result


def test_next_model_fallback_when_all_tried():
    used = [
        "openrouter/nvidia/nemotron-3-super-120b-a12b:free",
        "openrouter/minimax/minimax-m3",
        "openrouter/anthropic/claude-sonnet-4",
    ]
    result = next_model(used[-1], used)
    assert result == used[-1]  # falls back to current model


# --------------------------------------------------- attempt history ----


def test_consecutive_failures_counts_from_end():
    h = AttemptHistory(attempt=3, passed=[True, False, False])
    assert h.consecutive_failures == 2


def test_consecutive_failures_resets_after_pass():
    h = AttemptHistory(attempt=4, passed=[False, True, False, False])
    assert h.consecutive_failures == 2


def test_all_same_model_detects():
    h = AttemptHistory(attempt=2, passed=[False, False],
                       models_used=["gpt-4", "gpt-4"])
    assert h.all_same_model is True

    h2 = AttemptHistory(attempt=2, passed=[False, False],
                        models_used=["gpt-4", "claude"])
    assert h2.all_same_model is False


def test_has_pattern_matches_case_insensitive():
    h = AttemptHistory(attempt=1, diagnostics=["IMPORTERROR: no module"])
    assert h.has_pattern("importerror")
    assert not h.has_pattern("syntaxerror")


# -------------------------------------------------- workflow wiring (M3) ----
# The activity is the workflow-facing half of adaptive planning. It is pure
# (no store, no model calls), so it is testable directly.


def test_strategy_activity_first_failure_defaults_to_failure_context():
    from bucker.activities.pipeline import choose_adaptive_strategy

    result = asyncio.run(choose_adaptive_strategy({
        "attempt": 1,
        "objective": "fix the bug",
        "failure_context": "Attempt 1 failed verification.",
        "diagnostics": ["ImportError"],
        "passed": [False],
        "models_used": [""],
        "current_model": None,
    }))
    assert result["strategy"] == "default"
    assert "fix the bug" in result["next_objective"]
    assert "Attempt 1 failed verification." in result["next_objective"]
    assert "next_model" not in result


def test_strategy_activity_switch_model_after_repeated_same_model_failures():
    from bucker.activities.pipeline import choose_adaptive_strategy

    result = asyncio.run(choose_adaptive_strategy({
        "attempt": 2,
        "objective": "fix the bug",
        "failure_context": "nope",
        "diagnostics": ["ImportError", "ImportError"],
        "passed": [False, False],
        "models_used": ["", ""],     # both attempts on the default model
        "current_model": None,
    }))
    assert result["strategy"] == "switch_model"
    assert result["next_model"]  # a concrete model to switch to
    assert result["next_model"] != ""


def test_strategy_activity_chunk_after_models_exhausted():
    from bucker.activities.pipeline import choose_adaptive_strategy

    result = asyncio.run(choose_adaptive_strategy({
        "attempt": 2,
        "objective": "fix the bug",
        "failure_context": "nope",
        "diagnostics": ["ImportError: no module 'foo'", "no tests collected"],
        "passed": [False, False],
        "models_used": ["model-a", "model-b"],  # already switched once
        "current_model": "model-b",
    }))
    assert result["strategy"] == "chunk"
    assert "smaller steps" in result["next_objective"].lower()
    assert "ImportError" in result["next_objective"]


def test_strategy_activity_clarify_after_three_failures():
    from bucker.activities.pipeline import choose_adaptive_strategy

    result = asyncio.run(choose_adaptive_strategy({
        "attempt": 3,
        "objective": "fix the bug",
        "failure_context": "nope",
        "diagnostics": ["a", "b", "c"],
        "passed": [False, False, False],
        "models_used": ["", "", ""],
        "current_model": None,
    }))
    assert result["strategy"] == "clarify"
    assert "clarifying question" in result["next_objective"].lower()


def test_adaptive_flag_defaults_to_off():
    """Fixed retry stays the baseline; adaptive must be opted into (A/B)."""
    from bucker.workflows.code_task_workflow import CodeTaskInput

    inp = CodeTaskInput(task_id="t", objective="do the thing")
    assert inp.adaptive is False
