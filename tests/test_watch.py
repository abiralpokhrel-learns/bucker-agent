"""Watch-module tests (bucker.core.watch) + the CLI watch/wait commands.

The pure pieces (terminal classification, exit codes, formatting) are
table-tested; watch_task is exercised end-to-end against a live SQLite
event stream that grows WHILE it is being watched — the exact scenario a
user sees when tailing `bucker start --wait`-style tasks.
"""

from __future__ import annotations

import argparse
import asyncio
from uuid import uuid4

import pytest

from bucker.core.eventstore import EventStore, create_pool
from bucker.core.snapshots import SnapshotStore
from tests.conftest import make_event  # reuse the shared builder

# ------------------------------------------------------- classification ----


@pytest.mark.parametrize("status,terminal", [
    ("completed", True), ("failed", True), ("halted", True),
    ("needs_human_review", True), ("human_approved", True),
    ("human_rejected", True), ("cancelled", True),
    ("pending", False), ("running", False), (None, False), ("", False),
])
def test_is_terminal(status, terminal):
    from bucker.core.watch import is_terminal

    assert is_terminal(status) is terminal


@pytest.mark.parametrize("status,code", [
    ("completed", 0), ("human_approved", 0),
    ("needs_human_review", 2),
    ("failed", 1), ("halted", 1), ("cancelled", 1), (None, 1),
])
def test_exit_code_for(status, code):
    from bucker.core.watch import exit_code_for

    assert exit_code_for(status) == code


def test_format_event_line_matches_cli_style():
    from bucker.core.watch import format_event_line

    event = make_event(7, "TaskCompleted", {"attempt": 1})
    line = format_event_line(event)
    assert "TaskCompleted" in line
    assert "attempt" in line
    assert "7" in line.split()[0]


# ------------------------------------------------------------- watching ----


@pytest.fixture
def lite_db(tmp_path):
    def _dsn(name="watch.db"):
        return f"sqlite:///{(tmp_path / name).as_posix()}"
    return _dsn


async def _seed_task(pool, tid, objective="watched task"):
    await pool.execute(
        "INSERT INTO tasks (id, task_type, objective, status) "
        "VALUES ($1, 'demo', $2, 'pending')",
        tid, objective,
    )


async def test_watch_task_follows_stream_to_completion(lite_db):
    """Events appended by a background writer are picked up mid-watch —
    the live-tail behavior the CLI command promises."""
    from bucker.core.watch import watch_task

    dsn = lite_db()
    pool = await create_pool(dsn)
    try:
        store = EventStore(pool)
        snaps = SnapshotStore(pool, store)
        tid = uuid4()
        await pool.execute(
            "INSERT INTO tasks (id, task_type, objective, status) "
            "VALUES ($1, 'demo', 'watched task', 'pending')",
            tid,
        )
        await store.append(tid, "TaskCreated",
                           {"objective": "watched task"},
                           idempotency_key=f"{tid}:created")

        lines: list[str] = []

        async def slow_writer():
            # Append more events while the watcher is polling.
            for i in range(3):
                await asyncio.sleep(0.05)
                await store.append(tid, "StepCompleted", {"step": f"s{i}"},
                                   idempotency_key=f"{tid}:s{i}")
            await asyncio.sleep(0.05)
            await store.append(tid, "TaskCompleted", {},
                               idempotency_key=f"{tid}:done")

        writer = asyncio.create_task(slow_writer())
        status = await watch_task(store, snaps, tid,
                                  interval_s=0.02, timeout_s=10,
                                  sink=lines.append)
        await writer

        assert status == "completed"
        # The TaskCreated line plus every late-arriving event was printed.
        # (Column positions shift with id width; assert on content.)
        joined = "\n".join(lines)
        assert "TaskCreated" in joined
        assert joined.count("StepCompleted") == 3
        # The last printed line is the terminal event itself (the watcher
        # returns from the events it consumed — nothing vanishes between
        # read and verdict).
        assert "TaskCompleted" in lines[-1]
    finally:
        await pool.close()


async def test_watch_task_times_out_on_silent_task(lite_db):
    from bucker.core.watch import watch_task

    pool = await create_pool(lite_db())
    try:
        store = EventStore(pool)
        snaps = SnapshotStore(pool, store)
        status = await watch_task(store, snaps, uuid4(),
                                  interval_s=0.01, timeout_s=0.1,
                                  sink=lambda *_: None)
        assert status is None
    finally:
        await pool.close()


async def test_wait_for_status_polls_through_errors(lite_db):
    """get_state on an empty/unknown stream must not kill the wait loop."""
    from bucker.core.watch import wait_for_status

    pool = await create_pool(lite_db())
    try:
        store = EventStore(pool)
        snaps = SnapshotStore(pool, store)

        async def finish_later():
            await asyncio.sleep(0.05)
            await store.append(uuid4(), "TaskCreated", {})  # unrelated noise

        task = asyncio.create_task(finish_later())
        status = await wait_for_status(snaps, uuid4(),
                                       interval_s=0.01, timeout_s=0.2)
        await task
        assert status is None  # timed out quietly instead of raising
    finally:
        await pool.close()


# ------------------------------------------------------------------ CLI ----


def test_cli_watch_registered_with_flags():
    from bucker.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["watch", "12345678-1234-1234-1234-123456789abc",
                              "--interval", "0.5", "--timeout", "5"])
    assert args.command == "watch"
    assert args.interval == 0.5
    assert args.timeout == 5


def test_cli_wait_quiet_flag_parses():
    from bucker.cli import build_parser

    args = build_parser().parse_args(
        ["wait", "12345678-1234-1234-1234-123456789abc", "--quiet"]
    )
    assert args.quiet is True


def test_cli_watch_reports_verdict_and_exit_code(lite_db, capsys):
    """End-to-end through cmd_watch: seeded stream reaches completed; the
    command prints the verdict and exits 0. Sync on purpose — it drives
    its own event loop via asyncio.run, exactly like `bucker watch` does."""
    import bucker.config as cfg
    from bucker.cli import cmd_watch

    dsn = lite_db("cli.db")
    original_dsn = cfg.settings.database_url

    async def _run():
        object.__setattr__(cfg.settings, "database_url", dsn)
        try:
            pool = await create_pool(dsn)
            try:
                store = EventStore(pool)
                tid = uuid4()
                await pool.execute(
                    "INSERT INTO tasks (id, task_type, objective, status) "
                    "VALUES ($1, 'demo', 'cli watched', 'pending')",
                    tid,
                )
                await store.append(tid, "TaskCreated",
                                   {"objective": "cli watched"},
                                   idempotency_key=f"{tid}:created")
                await store.append(tid, "TaskCompleted", {},
                                   idempotency_key=f"{tid}:done")
                return await cmd_watch(argparse.Namespace(
                    task_id=str(tid), interval=0.01, timeout=1,
                ))
            finally:
                await pool.close()
        finally:
            object.__setattr__(cfg.settings, "database_url", original_dsn)

    code = asyncio.run(_run())
    captured = capsys.readouterr()
    assert code == 0
    assert "completed" in captured.out
