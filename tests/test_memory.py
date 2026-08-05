"""Memory system tests: semantic facts, procedural skills, consolidation,
trajectory export, and prompt injection.

Pure tests (tmp_path stores) plus one DB-gated consolidation test. The
prompt-injection tests prove the harness wiring: skills and facts become
part of the worker/planner prompts.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bucker.memory.facts import MemoryStore
from bucker.memory.skills import SkillStore, default_skill

# ------------------------------------------------------------ facts ----


def test_facts_roundtrip(tmp_path: Path):
    store = MemoryStore(tmp_path / "memory")
    fid = store.add("tests run with pytest", source="user")
    facts = store.list()
    assert len(facts) == 1
    assert facts[0]["id"] == fid
    assert facts[0]["text"] == "tests run with pytest"
    assert facts[0]["source"] == "user"
    assert store.get(fid)["text"] == "tests run with pytest"


def test_facts_reject_empty(tmp_path: Path):
    store = MemoryStore(tmp_path / "memory")
    with pytest.raises(ValueError):
        store.add("   ")


def test_facts_search_and_remove(tmp_path: Path):
    store = MemoryStore(tmp_path / "memory")
    store.add("tests run with pytest")
    store.add("the model is ollama/qwen2.5-coder:7b")
    hits = store.search("pytest")
    assert len(hits) == 1 and "pytest" in hits[0]["text"]
    assert store.search("nothing-here") == []
    assert store.remove(hits[0]["id"]) is True
    assert store.count() == 1


def test_facts_context_for_ranks_by_overlap(tmp_path: Path):
    store = MemoryStore(tmp_path / "memory")
    store.add("unrelated fact about weather")
    store.add("the test suite for calc.py uses pytest")
    store.add("pytest runs with coverage")
    ctx = store.context_for("make the pytest suite pass for calc.py")
    assert len(ctx) >= 2
    assert "pytest runs" in ctx[0]["text"] or "calc.py" in ctx[0]["text"]


def test_facts_are_git_ignored_by_default():
    """The memory dir is user-owned local data — never committed."""
    gitignore = Path(__file__).resolve().parent.parent / ".gitignore"
    assert "memory/" in gitignore.read_text(encoding="utf-8")


def test_prune_dedupes_identical_facts_keeping_newest(tmp_path: Path):
    store = MemoryStore(tmp_path / "memory")
    store.add("tests run with pytest")
    import time

    time.sleep(0.01)
    store.add("Tests run with PYTEST.")  # same normalized text, newer
    assert store.count() == 2
    removed = store.prune()
    assert len(removed) == 1
    remaining = store.list()
    assert len(remaining) == 1
    assert "PYTEST" in remaining[0]["text"]  # the newer one survived


def test_prune_caps_overflow_keeping_newest(tmp_path: Path):
    store = MemoryStore(tmp_path / "memory")
    for i in range(5):
        store.add(f"fact number {i} unique content")
    removed = store.prune(limit=2)
    assert len(removed) == 3
    remaining = [f["text"] for f in store.list()]
    assert any("fact number 4" in t for t in remaining)  # newest survives
    assert not any("fact number 0" in t for t in remaining)


def test_prune_never_merges_different_facts(tmp_path: Path):
    store = MemoryStore(tmp_path / "memory")
    store.add("tests run with pytest")
    store.add("the model is ollama")
    removed = store.prune()
    assert removed == []
    assert store.count() == 2


# ------------------------------------------------------------ skills ----


def test_skills_roundtrip(tmp_path: Path):
    store = SkillStore(tmp_path / "skills")
    store.add("fix-failing-tests",
              "Repair a failing test suite by reading errors first",
              "1. run tests\n2. read first failure\n3. fix root cause")
    skills = store.list()
    assert len(skills) == 1
    assert skills[0].name == "fix-failing-tests"
    assert "run tests" in skills[0].body
    assert store.get("fix-failing-tests").description.startswith("Repair")


def test_skill_name_must_be_slug(tmp_path: Path):
    store = SkillStore(tmp_path / "skills")
    with pytest.raises(ValueError):
        store.add("Bad Name!", "desc", "body")
    with pytest.raises(ValueError):
        store.add("x", "desc", "body")  # too short


def test_skills_match_objective(tmp_path: Path):
    store = SkillStore(tmp_path / "skills")
    store.add("fix-tests", "Repair a failing test suite",
              "1. run the tests")
    store.add("citation-report", "Write a cited research report",
              "1. gather sources")
    matched = store.for_objective("make the failing tests pass", limit=2)
    assert [s.name for s in matched] == ["fix-tests"]


def test_default_skill_is_sane():
    skill = default_skill()
    assert skill.name == "verify-before-done"
    assert "verifier" in skill.body.lower()


# ------------------------------------------------ prompt injection ----


def test_worker_prompt_injects_skills_and_facts(tmp_path: Path):
    """Procedural + semantic memory become part of working memory."""
    from bucker.memory.facts import MemoryStore
    from bucker.memory.skills import SkillStore
    from bucker.worker_agent import _facts_section, _skills_section

    SkillStore(tmp_path / "skills").add(
        "fix-tests", "Repair a failing test suite", "1. run the tests")
    MemoryStore(tmp_path / "memory").add("this project runs tests with pytest")

    orig_skill_root, orig_fact_root = SkillStore.default_root, MemoryStore.default_root
    try:
        SkillStore.default_root = tmp_path / "skills"
        MemoryStore.default_root = tmp_path / "memory"
        skills_section = _skills_section("make the failing tests pass")
        facts_section = _facts_section("make the pytest suite pass")
    finally:
        SkillStore.default_root, MemoryStore.default_root = orig_skill_root, orig_fact_root

    assert "fix-tests" in skills_section
    assert "pytest" in facts_section


def test_worker_prompt_renders_with_memory_placeholders(tmp_path: Path):
    """The full prompt renders with SKILLS + FACTS sections either way."""
    from bucker.contracts.models import Task
    from bucker.worker_agent import build_prompt

    task = Task(
        objective="make the failing tests pass",
        task_type="code_change",
        verifier="python_test_runner",
    )
    prompt = build_prompt(task, workspace_view="")
    assert "SKILLS" in prompt and "FACTS" in prompt


def test_planner_prompt_injects_facts(tmp_path: Path):
    from bucker.memory.facts import MemoryStore
    from bucker.planner import _facts_section

    MemoryStore(tmp_path / "memory").add("the project uses pytest")
    orig_root = MemoryStore.default_root
    try:
        MemoryStore.default_root = tmp_path / "memory"
        section = _facts_section("run the pytest suite")
    finally:
        MemoryStore.default_root = orig_root
    assert "pytest" in section


def test_empty_memory_produces_none_placeholders():
    from bucker.worker_agent import _facts_section, _skills_section

    assert _facts_section("anything") == "(none)"
    assert _skills_section("anything") == "(none)"


# ---------------------------------------------------- consolidation ----


class _FakeEvent:
    def __init__(self, event_type, payload, eid=1):
        self.event_type = event_type
        self.payload = payload
        self.id = eid
        self.created_at = None


async def test_consolidate_failed_task_yields_fact_and_proposal(tmp_path: Path):
    from bucker.memory.consolidate import ConsolidationStore, consolidate_task
    from bucker.memory.facts import MemoryStore

    class FakeStore:
        async def read_stream(self, task_id):
            return [
                _FakeEvent("TaskCreated", {
                    "objective": "add subtract to calc.py", "task_type": "code_change"}),
                _FakeEvent("VerificationFailed", {
                    "verifier": "python_test_runner", "attempt": 2,
                    "diagnostics": "1 failed, 2 passed"}),
            ]

    memory = MemoryStore(tmp_path / "memory")
    result = await consolidate_task(
        "00000000-0000-0000-0000-000000000001", FakeStore(), memory,
        markers=ConsolidationStore(tmp_path / "consolidated"),
    )
    assert len(result.facts_added) == 1
    assert "failed verification" in memory.list()[0]["text"]
    assert result.skill_proposals and result.skill_proposals[0]["name"] == \
        "repair-after-verification-failure"


async def test_consolidation_is_idempotent(tmp_path: Path):
    from bucker.memory.consolidate import ConsolidationStore, consolidate_task
    from bucker.memory.facts import MemoryStore

    class FakeStore:
        async def read_stream(self, task_id):
            return [
                _FakeEvent("TaskCreated", {"objective": "x", "task_type": "demo"}),
                _FakeEvent("VerificationPassed", {"verifier": "noop", "attempt": 1}),
            ]

    memory = MemoryStore(tmp_path / "memory")
    markers = ConsolidationStore(tmp_path / "consolidated")
    first = await consolidate_task(
        "00000000-0000-0000-0000-000000000002", FakeStore(), memory, markers=markers)
    second = await consolidate_task(
        "00000000-0000-0000-0000-000000000002", FakeStore(), memory, markers=markers)
    assert len(first.facts_added) == 1
    assert second.already_done is True
    assert memory.count() == 1  # not duplicated


# ---------------------------------------------------- trajectory ----


async def test_trajectory_summarizes_events(tmp_path: Path):
    from uuid import UUID

    from bucker.core.trajectory import (
        export_trajectory,
        trajectory_to_jsonl,
        trajectory_to_markdown,
    )

    class FakeStore:
        async def read_stream(self, task_id):

            return [
                _FakeEvent("TaskCreated", {"objective": "x"}),
                _FakeEvent("ModelCallCompleted", {
                    "purpose": "worker", "model": "ollama/qwen2.5-coder:7b",
                    "cost_usd": 0.0, "latency_ms": 100}),
                _FakeEvent("ToolCallCompleted", {"tool": "apply_diff", "exit_code": 0}),
                _FakeEvent("VerificationFailed", {
                    "verifier": "python_test_runner", "attempt": 1,
                    "diagnostics": "boom"}),
            ]

    traj = await export_trajectory(UUID(int=1), FakeStore())
    assert traj["summary"]["model_calls"] == 1
    assert traj["summary"]["failed_model_calls"] == 0
    assert traj["summary"]["verifications"] == 1
    md = trajectory_to_markdown(traj)
    assert "Trajectory" in md and "PASSED" not in md and "FAILED" in md
    jl = trajectory_to_jsonl(traj)
    assert jl.count("\n") == 4  # one line per event
