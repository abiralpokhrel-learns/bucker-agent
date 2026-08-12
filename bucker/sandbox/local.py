"""LocalSandbox — the no-Docker sandbox for lite mode.

Implements the same surface as ``DockerSandbox`` (start/stop/exec/
write_file/read_file/apply_diff, plus async context manager), but runs
commands as plain subprocesses in a scratch directory on the host.

Security posture (read before using):

* NO container isolation. The worker's code runs with the user's own
  permissions, filesystem, and network. Lite mode is for code you trust
  (your own tasks, demos, local experiments). Anything untrusted goes in
  the Docker sandbox, which stays the default.
* The scratch directory is created under the same blob root the full
  stack uses (``<blob_root>/workspace/<task_id>``), so a task's files
  are just as inspectable as in Docker mode.
* The same path-traversal guard as DockerSandbox: a model-authored path
  may not escape the workspace.
* ``apply_diff`` runs the same tolerance chain (git apply --recount,
  patch -p1, patch -p0) with the same diff-repair helpers.
* Every exec has a wall-clock timeout, same as the container path.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from bucker.sandbox.runtime import (
    DEFAULT_TIMEOUT_S,
    ExecResult,
    SandboxError,
    _remove_stray_prefix_dirs,
    ensure_diff_headers,
    repair_diff_prefixes,
)
from bucker.security.secrets import redact


class LocalSandbox:
    """One scratch directory per task; commands run as host subprocesses."""

    def __init__(
        self,
        workspace: Path,
        *,
        timeout_s: int = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.timeout_s = timeout_s
        self._started = False

    # ---------------------------------------------------------- lifecycle --
    async def start(self) -> None:
        """Nothing to start — the directory is the sandbox."""
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._started = True

    async def stop(self) -> None:
        self._started = False

    async def __aenter__(self) -> LocalSandbox:
        await self.start()
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.stop()

    # --------------------------------------------------------------- exec --
    async def exec(self, command: str, *, timeout_s: int | None = None) -> ExecResult:
        """Run a shell command in the scratch directory.

        The command runs through the platform shell (``sh -c`` on POSIX,
        ``cmd /c`` on Windows) with cwd = the task workspace. Output is
        captured verbatim and secret-scanned before it is returned, same
        as the container path.
        """
        if not self._started:
            raise SandboxError("sandbox not started")

        timeout = timeout_s or self.timeout_s

        # Pass the command as ONE argv element to the platform shell.
        # ``create_subprocess_shell(f"sh -c {command}")`` word-splits it:
        # ``sh -c echo hi`` runs ``echo`` with $0="hi" and prints nothing,
        # and ``sh -c git apply ...`` runs bare ``git``. exec keeps the
        # exact command string the caller intended.
        shell_argv = (
            ["cmd", "/c", command] if sys.platform == "win32"
            else ["sh", "-c", command]
        )

        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            proc = await asyncio.create_subprocess_exec(
                *shell_argv,
                cwd=str(self.workspace),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                timed_out = False
            except TimeoutError:
                proc.kill()
                await proc.wait()
                out, err = b"", f"timed out after {timeout}s".encode()
                timed_out = True
        except FileNotFoundError:
            raise SandboxError(
                "no shell found to run the sandbox command"
            ) from None

        duration_ms = int((loop.time() - started) * 1000)
        code = -1 if timed_out else (proc.returncode or 0)

        safe_out, found_out = redact(out.decode(errors="replace"))
        safe_err, found_err = redact(err.decode(errors="replace"))

        return ExecResult(
            command=command,
            exit_code=code,
            stdout=safe_out,
            stderr=safe_err,
            duration_ms=duration_ms,
            timed_out=timed_out,
            secret_findings=found_out + found_err,
        )

    # -------------------------------------------------------------- files --
    def _resolve(self, relative_path: str) -> Path:
        target = (self.workspace / relative_path).resolve()
        root = self.workspace.resolve()
        if not target.is_relative_to(root):
            raise SandboxError(f"path escapes workspace: {relative_path!r}")
        return target

    def write_file(self, relative_path: str, content: str) -> Path:
        """Write into the workspace. CRLF -> LF, same as the container path."""
        target = self._resolve(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="") as f:
            f.write(content.replace("\r\n", "\n"))
        return target

    def read_file(self, relative_path: str) -> str:
        return self._resolve(relative_path).read_text(encoding="utf-8")

    async def apply_diff(
        self, diff: str, *, files: list[str] | None = None
    ) -> ExecResult:
        """Apply a unified diff in the scratch dir — same tolerance chain."""
        diff = repair_diff_prefixes(ensure_diff_headers(diff, files))
        self.write_file(".bucker.patch", diff.replace("\r\n", "\n"))
        result = await self.exec(
            "git apply --recount --verbose .bucker.patch 2>&1 || "
            "patch -p1 --forward < .bucker.patch || "
            "patch -p0 --forward < .bucker.patch"
        )
        _remove_stray_prefix_dirs(self.workspace)
        return result


# -------------------------------------------------------------- factory ----
def make_sandbox(workspace: Path, *, local: bool = False):
    """Return the sandbox implementation for the current mode.

    ``local=True`` (lite mode, no Docker) -> LocalSandbox; otherwise the
    DockerSandbox from runtime.py. The caller cannot tell the difference.
    """
    from bucker.sandbox.runtime import DockerSandbox

    if local:
        return LocalSandbox(workspace)
    return DockerSandbox(workspace)
