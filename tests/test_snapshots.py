"""Snapshot equivalence (step 8).

The single invariant: snapshot + tail == full replay. Property-tested over
randomly generated streams, because the hand-picked cases are exactly the ones
where a bug hides.

Note the seeded RNG: these tests must be reproducible. A flaky property test
that fails once a month teaches you nothing.
"""

from __future__ import annotations

import random

import pytest

from bucker.core.events import EventType
from bucker.core.state import rebuild_state
from tests.conftest import make_event

SAFE_EVENTS = [
    (EventType.TASK_STARTED, lambda i: {}),
    (EventType.STEP_COMPLETED, lambda i: {"step": f"s{i}"}),
    (EventType.MODEL_CALL_COMPLETED, lambda i: {"cost_usd": round(i * 0.001, 6)}),
    (EventType.TOOL_CALL_COMPLETED, lambda i: {}),
    (EventType.VERIFICATION_FAILED, lambda i: {"diagnostics": f"fail {i}"}),
    (EventType.RETRY_SCHEDULED, lambda i: {"attempt": i}),
    (EventType.VERIFICATION_PASSED, lambda i: {"verifier": "noop"}),
]


def random_stream(rng: random.Random, length: int):
    events = [make_event(1, EventType.TASK_CREATED, {"objective": "seed" * 3})]
    for i in range(2, length + 1):
        event_type, payload = rng.choice(SAFE_EVENTS)
        events.append(make_event(i, event_type, payload(i)))
    return events


@pytest.mark.parametrize("seed", range(25))
def test_snapshot_plus_tail_equals_full_replay(seed: int):
    rng = random.Random(seed)
    events = random_stream(rng, rng.randint(2, 120))

    full = rebuild_state(events)

    cut = rng.randint(1, len(events) - 1)
    snapshot_state = rebuild_state(events[:cut])
    via_snapshot = rebuild_state(events[cut:], base=snapshot_state)

    assert via_snapshot == full, f"drift at cut={cut}, seed={seed}"


@pytest.mark.parametrize("seed", range(10))
def test_multiple_successive_snapshots_stay_consistent(seed: int):
    """Snapshot on top of snapshot on top of snapshot must not drift either."""
    rng = random.Random(1000 + seed)
    events = random_stream(rng, 90)

    full = rebuild_state(events)

    state = None
    for start in range(0, len(events), 20):
        chunk = events[start:start + 20]
        state = rebuild_state(chunk, base=state)

    assert state == full


def test_snapshot_of_empty_tail_is_identity():
    events = [make_event(1, EventType.TASK_CREATED, {"objective": "x" * 10})]
    state = rebuild_state(events)
    assert rebuild_state([], base=state) == state
