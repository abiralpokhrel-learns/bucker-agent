"""Sandbox tests (step 17).

The security properties live in the ``docker run`` arguments, so these assert
on ``build_run_args`` directly. That means the posture is checked on every CI
run, on machines with no Docker installed — rather than being a claim in a
comment that nobody re-reads.

If you ever delete one of these tests, write down in decisions.md why the risk
it covered became acceptable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bucker.sandbox.runtime import (
    DockerSandbox,
    SandboxError,
    build_run_args,
    docker_available,
    ensure_diff_headers,
    repair_diff_prefixes,
)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


def args_for(workspace: Path, **kwargs) -> list[str]:
    return build_run_args(container_name="bucker-test", workspace=workspace, **kwargs)


def flag_value(args: list[str], flag: str) -> str | None:
    return args[args.index(flag) + 1] if flag in args else None


# ------------------------------------------------------- security posture ---
def test_network_is_disabled_by_default(workspace):
    """The single most important flag. Contains exfiltration and callbacks."""
    assert flag_value(args_for(workspace), "--network") == "none"


def test_network_can_be_enabled_explicitly(workspace):
    """Opt-in only, and visible in the argv when it happens."""
    assert flag_value(args_for(workspace, network=True), "--network") == "bridge"


def test_all_capabilities_dropped(workspace):
    assert flag_value(args_for(workspace), "--cap-drop") == "ALL"


def test_no_new_privileges(workspace):
    assert flag_value(args_for(workspace), "--security-opt") == "no-new-privileges"


def test_runs_as_non_root(workspace):
    user = flag_value(args_for(workspace), "--user")
    assert user and not user.startswith("0:"), "container must not run as root"


def test_filesystem_is_read_only_outside_workspace(workspace):
    assert "--read-only" in args_for(workspace)


def test_tmp_is_capped_and_noexec(workspace):
    tmpfs = flag_value(args_for(workspace), "--tmpfs")
    assert "size=" in tmpfs, "/tmp must be size-capped or it can fill the disk"
    assert "noexec" in tmpfs


def test_only_the_workspace_is_mounted(workspace):
    """No host paths beyond the task's own workspace. No docker socket."""
    args = args_for(workspace)
    mounts = [args[i + 1] for i, a in enumerate(args) if a == "--volume"]

    assert len(mounts) == 1, f"expected exactly one mount, got {mounts}"
    assert mounts[0] == f"{workspace.resolve()}:/workspace"
    assert not any("docker.sock" in m for m in mounts)


def test_resource_limits_are_set(workspace):
    args = args_for(workspace)
    assert flag_value(args, "--memory")
    assert flag_value(args, "--cpus")
    assert flag_value(args, "--pids-limit")


def test_swap_is_disabled(workspace):
    """memory-swap equal to memory means no swap — a limit that can't be dodged."""
    args = args_for(workspace)
    assert flag_value(args, "--memory") == flag_value(args, "--memory-swap")


def test_never_privileged(workspace):
    args = args_for(workspace)
    assert "--privileged" not in args
    assert "--cap-add" not in args


def test_workdir_is_the_workspace(workspace):
    assert flag_value(args_for(workspace), "--workdir") == "/workspace"


# ------------------------------------------------------- path containment ---
@pytest.mark.parametrize("escape", [
    "../outside.txt",
    "../../etc/passwd",
    "subdir/../../escape.txt",
])
def test_write_rejects_path_traversal(workspace, escape):
    """A model-authored path must not reach the host filesystem."""
    sandbox = DockerSandbox(workspace)
    with pytest.raises(SandboxError, match="escapes workspace"):
        sandbox.write_file(escape, "malicious")


def test_read_rejects_path_traversal(workspace):
    sandbox = DockerSandbox(workspace)
    with pytest.raises(SandboxError, match="escapes workspace"):
        sandbox.read_file("../../etc/passwd")


def test_sibling_directory_prefix_is_not_inside_the_workspace(tmp_path):
    """The classic startswith failure: 'workspace2/x' starts with 'workspace'.

    is_relative_to must reject it even though the string prefix matches.
    """
    ws = tmp_path / "workspace"
    ws.mkdir()
    sibling = tmp_path / "workspace2"
    sibling.mkdir()

    sandbox = DockerSandbox(ws)
    with pytest.raises(SandboxError, match="escapes workspace"):
        sandbox.write_file("../workspace2/evil.txt", "malicious")
    assert not (sibling / "evil.txt").exists()  # nothing written outside


def test_legitimate_nested_write_works(workspace):
    sandbox = DockerSandbox(workspace)
    sandbox.write_file("src/module/file.py", "print('ok')\n")
    assert sandbox.read_file("src/module/file.py") == "print('ok')\n"


def test_exec_before_start_is_an_error(workspace):
    sandbox = DockerSandbox(workspace)

    async def run():
        await sandbox.exec("echo hi")

    with pytest.raises(SandboxError, match="not started"):
        import asyncio
        asyncio.run(run())


def test_container_names_are_unique(workspace):
    a, b = DockerSandbox(workspace), DockerSandbox(workspace)
    assert a.container_name != b.container_name


# ------------------------------------------------- diff header inference ---
# Small models sometimes emit a bare hunk with no ---/+++ file headers.
# Neither git apply nor patch can guess the target file; the worker's
# files_touched names it, so the headers are prepended.


def test_bare_hunk_gets_headers_from_files_touched():
    diff = "@@ -1,3 +1,6 @@\n def add(a, b):\n     return a + b\n+def sub(a, b):\n"
    fixed = ensure_diff_headers(diff, ["calc.py"])
    assert fixed.startswith("--- a/calc.py\n+++ b/calc.py\n@@ -1,3")
    assert diff in fixed  # content preserved


def test_existing_headers_are_left_alone():
    diff = "--- a/calc.py\n+++ b/calc.py\n@@ -1,3 +1,6 @@\n"
    assert ensure_diff_headers(diff, ["calc.py"]) == diff


def test_diff_git_format_is_left_alone():
    diff = "diff --git a/calc.py b/calc.py\n--- a/calc.py\n+++ b/calc.py\n"
    assert ensure_diff_headers(diff, ["calc.py"]) == diff


def test_ambiguous_cases_are_never_guessed():
    hunk = "@@ -1,3 +1,6 @@\n def add(a, b):\n"
    # No files at all.
    assert ensure_diff_headers(hunk, None) == hunk
    # Multiple files — cannot know which one the bare hunk targets.
    assert ensure_diff_headers(hunk, ["a.py", "b.py"]) == hunk


def test_leading_blank_lines_are_stripped_before_headers():
    diff = "\n\n@@ -1,3 +1,6 @@\n def add(a, b):\n"
    fixed = ensure_diff_headers(diff, ["calc.py"])
    assert fixed.startswith("--- a/calc.py\n+++ b/calc.py\n@@")
    assert "\n\n@@ " not in fixed


# ------------------------------------------------- diff prefix repair ------
# Small models occasionally drop the "+" on an added line inside a hunk,
# which makes git apply call the patch corrupt.


def test_missing_plus_prefix_is_restored():
    diff = (
        "@@ -1,3 +1,6 @@\n def add(a, b):\n     return a + b\n+\n"
        "def sub(a, b):\n+    return a - b\n"
    )
    fixed = repair_diff_prefixes(diff)
    assert "+def sub(a, b):" in fixed
    added_lines = [line for line in fixed.split("\n") if line.startswith("+")]
    assert len(added_lines) == 3  # blank add, def line, return line


def test_valid_hunk_is_untouched():
    diff = "@@ -1,3 +1,6 @@\n def add(a, b):\n+def sub(a, b):\n"
    assert repair_diff_prefixes(diff) == diff


def test_headers_and_separators_are_untouched():
    diff = (
        "--- a/calc.py\n+++ b/calc.py\n@@ -1,3 +1,6 @@\n"
        " def add(a, b):\n+def sub(a, b):\n\n"
        "--- a/other.py\n+++ b/other.py\n@@ -1,1 +1,1 @@\n-x\n+y\n"
    )
    fixed = repair_diff_prefixes(diff)
    assert fixed.startswith("--- a/calc.py")
    hunk_lines = [line for line in fixed.split("\n") if line.startswith("@@")]
    assert len(hunk_lines) == 2  # both hunks intact
    assert "-x\n+y\n" in fixed


def test_no_newline_marker_is_untouched():
    diff = "@@ -1,2 +1,2 @@\n def a():\n-    x\n+    y\n\\ No newline at end of file\n"
    fixed = repair_diff_prefixes(diff)
    assert "\\ No newline" in fixed
    added_lines = [line for line in fixed.split("\n") if line.startswith("+")]
    assert len(added_lines) == 1  # only the genuine addition


# ---------------------------------------------------------- integration -----
# Only runs where Docker actually exists.
async def test_real_container_roundtrip(workspace):
    if not await docker_available():
        pytest.skip("docker not available")

    async with DockerSandbox(workspace) as sandbox:
        result = await sandbox.exec("echo hello from the sandbox")
        assert result.ok
        assert "hello from the sandbox" in result.stdout


async def test_real_container_has_no_network(workspace):
    """The containment claim, verified against a real container."""
    if not await docker_available():
        pytest.skip("docker not available")

    async with DockerSandbox(workspace) as sandbox:
        result = await sandbox.exec(
            "python -c \"import socket; socket.create_connection(('1.1.1.1', 80), 3)\""
        )
        assert not result.ok, "sandbox reached the network — containment is broken"


# --------------------------------------------------- start-retry (iter 2) --


async def test_start_retries_transient_docker_failures(monkeypatch, tmp_path):
    """A hiccuping `docker run` must not kill the task: bounded retry."""
    import bucker.sandbox.runtime as rt

    calls = {"n": 0}

    async def flaky_run(args, timeout_s=120):
        calls["n"] += 1
        if calls["n"] < 3:
            return 125, "", "daemon is warming up"  # transient
        return 0, "ok", ""

    monkeypatch.setattr(rt, "_run", flaky_run)
    sandbox = DockerSandbox(tmp_path / "ws", image="test-image")
    await sandbox.start()
    assert sandbox._started is True
    assert calls["n"] == 3, "must have retried twice then succeeded"


async def test_start_gives_up_after_three_attempts(monkeypatch, tmp_path):
    import bucker.sandbox.runtime as rt

    calls = {"n": 0}

    async def always_fail(args, timeout_s=120):
        calls["n"] += 1
        return 125, "", "boom"

    monkeypatch.setattr(rt, "_run", always_fail)
    sandbox = DockerSandbox(tmp_path / "ws", image="test-image")
    with pytest.raises(SandboxError, match="after 3 attempts"):
        await sandbox.start()
    assert calls["n"] == 3
