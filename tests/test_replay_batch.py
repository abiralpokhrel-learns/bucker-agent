"""Batch replay tests (bucker.replay.batch).

The real replay engine needs blobs + recordings; these tests inject a
fake replay_fn so the BATCH semantics are what is under test: bucketing,
error isolation, match-rate math, and the id-selection query.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from bucker.core.eventstore import EventStore, create_pool
from bucker.replay.batch import (
    BatchReplayReport,
    format_batch_report,
    replay_batch,
)


def make_replay_fn(outcomes: dict[str, object]):
    """A replay_task stand-in keyed by task-id prefix.

    Outcome strings: "match" | "mismatch"; an Exception value is raised.
    """

    async def _replay(tid, *, store=None, blobs=None):
        outcome = outcomes.get(str(tid)[:8])
        if isinstance(outcome, Exception):
            raise outcome

        class _Result:
            match = outcome == "match"
            original_passed = True
            replayed_passed = outcome == "match"
            diagnostics = "fake diagnostics"

        return _Result()

    return _replay


async def seed_tasks(pool, statuses: list[str]) -> list[str]:
    ids = []
    for status in statuses:
        tid = uuid4()
        await pool.execute(
            "INSERT INTO tasks (id, task_type, objective, status) "
            "VALUES ($1, 'demo', 'batch fixture', $2)",
            tid, status,
        )
        ids.append(str(tid))
    return ids


def test_match_rate_math():
    report = BatchReplayReport(requested=4)
    report.matched = ["a", "b", "c"]
    report.mismatched = [{"task_id": "d"}]
    assert report.attempted == 4
    assert report.match_rate == pytest.approx(0.75)
    assert "75%" in report.summary_line()
    assert "3 match" in report.summary_line()


def test_match_rate_none_when_nothing_checked():
    assert BatchReplayReport().match_rate is None


async def test_batch_buckets_outcomes(tmp_path):
    dsn = f"sqlite:///{(tmp_path / 'batch.db').as_posix()}"
    pool = await create_pool(dsn)
    store = EventStore(pool)
    try:
        ids = await seed_tasks(pool, ["completed"] * 3)
        outcomes = {
            ids[0][:8]: "match",
            ids[1][:8]: "mismatch",
            ids[2][:8]: ReplayFailure("recordings missing"),
        }
        report = await replay_batch(
            pool, store, blobs=object(),
            task_ids=ids, replay_fn=make_replay_fn(outcomes),
        )
        assert report.requested == 3
        assert report.matched == [ids[0]]
        assert len(report.mismatched) == 1
        # The failure was isolated into its own bucket, not a mismatch.
        assert len(report.errors) == 1
        assert "recordings missing" in report.errors[0]["reason"]
        assert report.match_rate == pytest.approx(0.5)
    finally:
        await pool.close()


class ReplayFailure(Exception):
    pass


async def test_batch_selects_recent_completed_by_default(tmp_path):
    """Without explicit ids the batch targets newest-first tasks in the
    requested terminal status — the `--recent N` CLI contract."""
    dsn = f"sqlite:///{(tmp_path / 'sel.db').as_posix()}"
    pool = await create_pool(dsn)
    store = EventStore(pool)
    try:
        await seed_tasks(pool, ["completed"] * 3)
        await seed_tasks(pool, ["failed"])          # wrong status: excluded

        seen: list[str] = []

        async def _replay(tid, *, store=None, blobs=None):
            seen.append(str(tid))
            raise ReplayFailure("stop")

        report = await replay_batch(pool, store, blobs=object(),
                                    limit=2, replay_fn=_replay)
        assert report.requested == 2                # limit honored
        assert len(seen) == 2
        assert all(r["reason"] for r in report.errors)  # all errored cleanly
    finally:
        await pool.close()


def test_format_report_lists_mismatch_and_errors():
    report = BatchReplayReport(requested=2)
    report.matched = ["aaaaaaaa-1"]
    report.mismatched = [{
        "task_id": "bbbbbbbb-2",
        "original_passed": True,
        "replayed_passed": False,
        "diagnostics": "verdict diverged",
    }]
    report.errors = [{"task_id": "cccccccc-3", "reason": "no recordings"}]
    text = format_batch_report(report)
    assert "MISMATCH bbbbbbbb" in text
    assert "verdict diverged" in text
    assert "ERROR    cccccccc" in text
    assert "no recordings" in text
