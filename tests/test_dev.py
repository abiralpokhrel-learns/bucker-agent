"""Usability-pass tests: the one-command setup/dev planning logic.

Pure functions only — nothing is spawned, no ports are touched (the
port/dependency checks are monkeypatched).
"""

from __future__ import annotations

from pathlib import Path


def test_ensure_env_creates_with_token(tmp_path: Path):
    from bucker.dev import ensure_env

    example = tmp_path / ".env.example"
    example.write_text("BUCKER_API_TOKEN=dev-token\nBUCKER_MODEL=ollama/qwen2.5-coder:7b\n",
                       encoding="utf-8")
    target = tmp_path / ".env"

    assert ensure_env(example, target) == "created"
    content = target.read_text(encoding="utf-8")
    assert "BUCKER_API_TOKEN=" in content
    assert content != "BUCKER_API_TOKEN=dev-token\n"  # token was replaced
    assert "dev-token" not in content


def test_ensure_env_never_overwrites(tmp_path: Path):
    from bucker.dev import ensure_env

    example = tmp_path / ".env.example"
    example.write_text("BUCKER_API_TOKEN=dev-token\n", encoding="utf-8")
    target = tmp_path / ".env"
    target.write_text("my precious values\n", encoding="utf-8")

    assert ensure_env(example, target) == "exists"
    assert target.read_text(encoding="utf-8") == "my precious values\n"


def test_plan_stack_skips_running_services(monkeypatch):
    import bucker.dev as dev

    monkeypatch.setattr(dev, "port_open", lambda host="127.0.0.1", port=0,
                        timeout=1.0: port in (5432, 8233, 8123))
    monkeypatch.setattr(dev, "_has", lambda cmd: cmd == "docker")
    plan = dev.plan_stack()
    assert plan["postgres"] == "running"
    assert plan["temporal"] == "running"
    assert plan["api"] == "running"
    assert plan["worker"] == "start"


def test_plan_stack_starts_everything_when_nothing_running(monkeypatch):
    import bucker.dev as dev

    monkeypatch.setattr(dev, "port_open", lambda host="127.0.0.1", port=0,
                        timeout=1.0: False)
    monkeypatch.setattr(dev, "_has", lambda cmd: cmd in ("docker", "temporal"))
    plan = dev.plan_stack()
    assert plan["postgres"] == "start"
    assert plan["temporal"] == "start-cli"
    assert plan["api"] == "start"
    assert plan["worker"] == "start"


def test_plan_stack_temporal_falls_back_to_docker(monkeypatch):
    import bucker.dev as dev

    monkeypatch.setattr(dev, "port_open", lambda host="127.0.0.1", port=0,
                        timeout=1.0: False)
    monkeypatch.setattr(dev, "_has", lambda cmd: cmd == "docker")  # no temporal CLI
    plan = dev.plan_stack()
    assert plan["temporal"] == "start-docker"  # docker image fallback
    assert plan["needs_docker"] is True


def test_plan_stack_reports_missing_prerequisites(monkeypatch):
    import bucker.dev as dev

    monkeypatch.setattr(dev, "port_open", lambda host="127.0.0.1", port=0,
                        timeout=1.0: False)
    monkeypatch.setattr(dev, "_has", lambda cmd: False)  # nothing installed
    plan = dev.plan_stack()
    assert plan["postgres"] == "missing-docker"
    assert plan["temporal"] == "missing"
