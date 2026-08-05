"""Graph engineering tests: DAG validation, parallel waves, API.

Pure tests for the spec contract + a wiring test proving GraphWorkflow
is registered on the worker, plus API validation paths.
"""

from __future__ import annotations

import pytest

from bucker.contracts.graph import (
    parse_spec,
    topological_waves,
    validate_graph,
)


def _diamond() -> dict:
    """a -> {b, c} -> d: classic parallel-branch DAG."""
    return {
        "name": "diamond",
        "steps": [
            {"id": "a", "objective": "step a"},
            {"id": "b", "objective": "step b", "depends_on": ["a"]},
            {"id": "c", "objective": "step c", "depends_on": ["a"]},
            {"id": "d", "objective": "step d", "depends_on": ["b", "c"]},
        ],
    }


# ------------------------------------------------------------ parsing ----


def test_parse_spec_roundtrip():
    spec = parse_spec(_diamond())
    assert spec.name == "diamond"
    assert len(spec.steps) == 4
    assert spec.steps[1].depends_on == ["a"]
    assert spec.steps[1].verifier == "python_test_runner"


def test_parse_spec_rejects_malformed():
    with pytest.raises(ValueError, match="name"):
        parse_spec({"steps": []})
    with pytest.raises(ValueError, match="steps"):
        parse_spec({"name": "x", "steps": []})
    with pytest.raises(ValueError, match="objective"):
        parse_spec({"name": "x", "steps": [{"id": "a"}]})
    with pytest.raises(ValueError, match="must be a JSON object"):
        parse_spec([])


# ------------------------------------------------------------ validity ----


def test_valid_graph_has_no_errors():
    assert validate_graph(parse_spec(_diamond())) == []


def test_duplicate_ids_rejected():
    spec = parse_spec({"name": "x", "steps": [
        {"id": "a", "objective": "1"},
        {"id": "a", "objective": "2"},
    ]})
    assert any("duplicate" in e for e in validate_graph(spec))


def test_unknown_dependency_rejected():
    spec = parse_spec({"name": "x", "steps": [
        {"id": "a", "objective": "1", "depends_on": ["ghost"]},
    ]})
    assert any("unknown step" in e for e in validate_graph(spec))


def test_cycle_rejected():
    spec = parse_spec({"name": "x", "steps": [
        {"id": "a", "objective": "1", "depends_on": ["b"]},
        {"id": "b", "objective": "2", "depends_on": ["a"]},
    ]})
    assert any("cycle" in e for e in validate_graph(spec))


def test_self_dependency_rejected():
    spec = parse_spec({"name": "x", "steps": [
        {"id": "a", "objective": "1", "depends_on": ["a"]},
    ]})
    assert any("cycle" in e for e in validate_graph(spec))


def test_bad_budgets_rejected():
    spec = parse_spec({"name": "x", "budget_usd": -1, "steps": [
        {"id": "a", "objective": "1", "budget_usd": 0},
    ]})
    errors = validate_graph(spec)
    assert any("budget" in e for e in errors)


# ------------------------------------------------------------ waves ----


def test_waves_parallelize_independent_steps():
    spec = parse_spec(_diamond())
    waves = topological_waves(spec)
    # a alone, then b and c in parallel, then d.
    assert waves == [["a"], ["b", "c"], ["d"]]


def test_waves_for_linear_graph():
    spec = parse_spec({"name": "linear", "steps": [
        {"id": "a", "objective": "1"},
        {"id": "b", "objective": "2", "depends_on": ["a"]},
        {"id": "c", "objective": "3", "depends_on": ["b"]},
    ]})
    assert topological_waves(spec) == [["a"], ["b"], ["c"]]


def test_waves_for_fanout():
    spec = parse_spec({"name": "fanout", "steps": [
        {"id": "a", "objective": "1"},
        {"id": "b", "objective": "2", "depends_on": ["a"]},
        {"id": "c", "objective": "3", "depends_on": ["a"]},
        {"id": "d", "objective": "4", "depends_on": ["a"]},
    ]})
    assert topological_waves(spec) == [["a"], ["b", "c", "d"]]


def test_waves_are_deterministic():
    assert topological_waves(parse_spec(_diamond())) == \
        topological_waves(parse_spec(_diamond()))


# ------------------------------------------------------------ wiring ----


def test_graph_workflow_is_importable_and_registered():
    import bucker.worker as worker_mod
    from bucker.workflows.graph_workflow import GraphInput, GraphWorkflow

    # The worker module must reference GraphWorkflow for registration.
    assert "GraphWorkflow" in dir(worker_mod)
    assert GraphInput.__name__ == "GraphInput"
    assert GraphWorkflow.__name__ == "GraphWorkflow"


def test_activity_return_annotations_match_tuple_returns():
    """Regression: Temporal decodes activity results by the defn's return
    annotation. plan_task/run_worker return (dict, cost) tuples — an
    annotation of `dict` makes every live run fail with
    'Expected dict, value was list' (caught live on the first graph run).
    """
    import typing

    from bucker.activities.pipeline import run_worker
    from bucker.activities.planner import plan_task

    for fn in (plan_task, run_worker):
        # With `from __future__ import annotations` the raw signature shows
        # a string — resolve it to the real type.
        annotation = typing.get_type_hints(fn).get("return")
        assert annotation == tuple[dict, float, bool], (
            f"{fn.__name__} must annotate -> tuple[dict, float, bool] so Temporal "
            f"can decode the (dict, cost, cost_unknown) result"
        )
