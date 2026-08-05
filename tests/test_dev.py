"""Usability-pass tests: the one-command setup/dev planning logic.

Pure functions only — nothing is spawned, no ports are touched (the
port/dependency checks are monkeypatched).
"""

from __future__ import annotations

import asyncio
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


# ------------------------------------------------- first-run detection -----
# `bucker dev` bootstraps when the machine is not ready: no .env, no
# database, or migrations not applied. Pure logic — all probes mocked.


async def _ready() -> bool:
    return True


def test_first_run_needed_missing_env(monkeypatch, tmp_path):
    import bucker.dev as dev

    monkeypatch.setattr(dev, "PROJECT_ROOT", tmp_path)          # no .env
    monkeypatch.setattr(dev, "port_open", lambda **kw: True)
    monkeypatch.setattr(dev, "_db_migrated", _ready)
    assert asyncio.run(dev.first_run_needed()) is True


def test_first_run_needed_unmigrated_db(monkeypatch, tmp_path):
    import bucker.dev as dev

    (tmp_path / ".env").write_text("BUCKER_API_TOKEN=x\n", encoding="utf-8")
    monkeypatch.setattr(dev, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(dev, "port_open", lambda **kw: True)    # postgres up
    monkeypatch.setattr(dev, "_db_migrated", lambda: asyncio.sleep(0) or False)
    assert asyncio.run(dev.first_run_needed()) is True


def test_first_run_needed_false_when_ready(monkeypatch, tmp_path):
    import bucker.dev as dev

    (tmp_path / ".env").write_text("BUCKER_API_TOKEN=x\n", encoding="utf-8")
    monkeypatch.setattr(dev, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(dev, "port_open", lambda **kw: True)
    monkeypatch.setattr(dev, "_db_migrated", _ready)
    assert asyncio.run(dev.first_run_needed()) is False


# -------------------------------------------------- one-command flow -----


async def _setup_fail() -> int:
    return 1


async def _stack(**kw) -> int:
    return 0


def test_run_dev_bootstraps_then_starts(monkeypatch):
    import bucker.dev as dev

    calls: list[str] = []
    async def _setup():
        calls.append("setup")
        return 0
    monkeypatch.setattr(dev, "first_run_needed", _ready)        # True
    monkeypatch.setattr(dev, "run_setup", _setup)
    monkeypatch.setattr(dev, "run_stack", _stack)
    rc = asyncio.run(dev.run_dev(live_models=False, open_browser=False))
    assert rc == 0
    assert calls == ["setup"]  # setup ran before the stack


def test_run_dev_aborts_when_setup_fails(monkeypatch):
    import bucker.dev as dev

    monkeypatch.setattr(dev, "first_run_needed", _ready)
    monkeypatch.setattr(dev, "run_setup", _setup_fail)
    stack_called = False
    async def _stack(**kw):
        nonlocal stack_called
        stack_called = True
        return 0
    monkeypatch.setattr(dev, "run_stack", _stack)
    rc = asyncio.run(dev.run_dev())
    assert rc == 1
    assert stack_called is False  # never start the stack on a failed setup


def test_run_dev_skips_setup_when_ready(monkeypatch):
    import bucker.dev as dev

    setup_called = False
    monkeypatch.setattr(dev, "first_run_needed",
                        lambda: asyncio.sleep(0) or False)
    async def _setup():
        nonlocal setup_called
        setup_called = True
        return 0
    monkeypatch.setattr(dev, "run_setup", _setup)
    stack_kw: dict = {}
    async def _stack(**kw):
        stack_kw.update(kw)
        return 0
    monkeypatch.setattr(dev, "run_stack", _stack)
    rc = asyncio.run(dev.run_dev(live_models=False, open_browser=False))
    assert rc == 0
    assert setup_called is False
    assert stack_kw == {"live_models": False, "open_browser": False}


def test_run_dev_force_setup_runs_even_when_ready(monkeypatch):
    import bucker.dev as dev

    setup_called = False
    monkeypatch.setattr(dev, "first_run_needed",
                        lambda: asyncio.sleep(0) or False)
    async def _setup():
        nonlocal setup_called
        setup_called = True
        return 0
    monkeypatch.setattr(dev, "run_setup", _setup)
    monkeypatch.setattr(dev, "run_stack", _stack)
    asyncio.run(dev.run_dev(force_setup=True))
    assert setup_called is True  # --force-setup overrides detection


def test_has_uv_finds_off_path_install(monkeypatch, tmp_path):
    """uv freshly installed via winget/curl may be off-PATH for the current
    shell; the known home locations must still count as 'installed'."""
    import bucker.dev as dev

    monkeypatch.setattr(dev, "_has", lambda cmd: False)
    fake = tmp_path / "bin" / "uv"
    fake.parent.mkdir()
    fake.write_text("", encoding="utf-8")
    monkeypatch.setattr(dev, "_uv_install_path", lambda: fake)
    assert dev._has_uv() is True
