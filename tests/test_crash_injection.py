"""The crash injection must fire exactly once (regression test).

Bug this locks down: `crash_at` travels in the workflow input, and Temporal
gives a retried activity the identical input. The first version of this logic
therefore killed every replacement worker too, so the task never resumed and
the M1 crash test timed out with no progress at all.

Cheap test, expensive bug — it cost a full 180-second run to diagnose by hand.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bucker.activities.demo import should_inject_crash


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace" / "task-1"
    ws.mkdir(parents=True)
    return ws


def test_fires_on_the_targeted_step(workspace):
    assert should_inject_crash(workspace, "transform", "transform") is True


def test_fires_only_once_across_retries(workspace):
    """The regression. Retry 1 crashes; every later retry must proceed."""
    assert should_inject_crash(workspace, "transform", "transform") is True
    for attempt in range(2, 6):
        assert should_inject_crash(workspace, "transform", "transform") is False, (
            f"crash injected again on attempt {attempt} — replacement workers "
            f"would die forever and the task could never resume"
        )


def test_leaves_a_durable_marker(workspace):
    """Must survive process death, so it cannot be an in-memory flag."""
    should_inject_crash(workspace, "transform", "transform")
    assert (workspace / "transform.crashed").exists()


def test_ignores_other_steps(workspace):
    for step in ("fetch", "analyze", "validate", "publish"):
        assert should_inject_crash(workspace, step, "transform") is False


def test_no_crash_when_unset(workspace):
    """Normal runs must never crash, whatever the step."""
    for step in ("fetch", "analyze", "transform", "validate", "publish"):
        assert should_inject_crash(workspace, step, None) is False
        assert should_inject_crash(workspace, step, "") is False


def test_crash_marker_does_not_pollute_done_markers(workspace):
    """The final assertions glob '*.done'; the crash marker must not match."""
    should_inject_crash(workspace, "transform", "transform")
    (workspace / "transform.done").write_text("x")
    assert sorted(p.name for p in workspace.glob("*.done")) == ["transform.done"]
