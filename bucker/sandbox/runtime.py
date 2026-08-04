"""Sandboxed tool runtime — where untrusted work actually runs (step 17).

[HAND] — this is the security boundary. A worker executes model-authored code
here; if the container is loose, "the model wrote a script" becomes "the model
ran a script on your machine with your credentials."

The threat model from the Security Assessment, made concrete:

  * **Malicious code execution** -> isolated container per task, all Linux
    capabilities dropped, no privilege escalation, no host mounts beyond the
    task's own workspace.
  * **Data exfiltration** -> ``--network none`` by default. A task that needs
    the network must opt in explicitly and visibly.
  * **Runaway resource use** -> memory, CPU, and PID limits, plus a wall-clock
    timeout on every exec.
  * **Prompt injection via tool output** -> handled upstream: output is data,
    never instructions. Here we only guarantee it is captured verbatim and
    secret-scanned before storage.

Testability note: the security properties live entirely in the ``docker run``
arguments, so ``build_run_args`` is a pure function and the test suite asserts
on its output. That means the posture is verified on every CI run, on machines
without Docker, rather than being a claim in a comment.
"""

from __future__ import annotations

import asyncio
import shlex
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from bucker.config import settings
from bucker.security.secrets import Finding, redact

#: Built from Dockerfile.sandbox — python:3.12-slim plus pytest, ruff, and git.
#: It has to carry every tool a verifier needs, because containers run with no
#: network and nothing can be installed at task time. Pin by digest before
#: publishing any benchmark numbers (Phase 3).
DEFAULT_IMAGE = settings.sandbox_image

#: Ceilings, not targets. A task needing more should say so in its contract.
DEFAULT_MEMORY = "512m"
DEFAULT_CPUS = "1.0"
DEFAULT_PIDS = 256
DEFAULT_TIMEOUT_S = 120


class SandboxError(Exception):
    """The sandbox itself failed — distinct from the command failing."""


@dataclass(frozen=True, slots=True)
class ExecResult:
    """One command's outcome, captured verbatim (then redacted for storage)."""

    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False
    secret_findings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


def build_run_args(
    *,
    container_name: str,
    workspace: Path,
    image: str = DEFAULT_IMAGE,
    network: bool = False,
    memory: str = DEFAULT_MEMORY,
    cpus: str = DEFAULT_CPUS,
    pids: int = DEFAULT_PIDS,
) -> list[str]:
    """Build the ``docker run`` argv. Pure — this is what the tests assert on.

    Every flag here is load-bearing. If you remove one, remove the matching
    test too, and write down in decisions.md why the risk it covered is
    acceptable now.
    """
    args = [
        "docker", "run",
        "--detach",
        "--name", container_name,

        # No network unless explicitly requested. This is the single most
        # important line in the file: it turns "the model wrote something
        # malicious" into a local, contained problem.
        "--network", "none" if not network else "bridge",

        # Drop every capability, then add nothing back. A task that legitimately
        # needs one should fail loudly and be discussed, not silently granted.
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",

        # Resource ceilings — a runaway loop must not take the host down.
        "--memory", memory,
        "--memory-swap", memory,          # equal to memory => swap disabled
        "--cpus", cpus,
        "--pids-limit", str(pids),

        # The ONLY host path visible inside. Not the repo, not the home
        # directory, not the Docker socket.
        "--volume", f"{workspace.resolve()}:/workspace",
        "--workdir", "/workspace",

        # Non-root inside the container as well as unprivileged outside it.
        "--user", "1000:1000",

        # Writable only where work happens; /tmp capped so it cannot fill disk.
        "--read-only",
        "--tmpfs", "/tmp:rw,size=64m,noexec",

        image,
        # Keep the container alive so exec can be called repeatedly; the
        # workflow controls the lifetime, not the command.
        "sleep", "infinity",
    ]
    return args


def ensure_diff_headers(diff: str, files: list[str] | None) -> str:
    """Prepend ``---``/``+++`` file headers when the model omitted them.

    Small models sometimes emit a bare hunk with no file headers at all.
    Neither ``git apply`` nor ``patch`` can guess the target file. The
    worker's ``files_touched`` names it unambiguously for a single-file
    diff, so when headers are missing and exactly one file is known, they
    are prepended. Anything ambiguous is left unchanged — the verifier,
    not the repair, is the judge of correctness.
    """
    if not files or len(files) != 1:
        return diff
    stripped = diff.lstrip("\n")
    first = stripped.split("\n", 1)[0].strip()
    if first.startswith(("---", "+++", "diff ", "Index:")):
        return diff
    return f"--- a/{files[0]}\n+++ b/{files[0]}\n{stripped}"


def repair_diff_prefixes(diff: str) -> str:
    """Restore the ``+`` prefix on hunk lines that lost it.

    Small models occasionally emit an added line without its ``+`` prefix —
    typically the line right after a blank addition. git apply rejects
    unprefixed hunk lines as corrupt. Inside a hunk, any line with no
    ``+``/``-``/space prefix is treated as an addition. If the repair is
    wrong, the hunk fails to apply and the verifier catches it — the
    verifier, not this function, decides correctness.
    """
    out: list[str] = []
    in_hunk = False
    for line in diff.split("\n"):
        if line.startswith("@@"):
            in_hunk = True
            out.append(line)
            continue
        if in_hunk and line == "":
            in_hunk = False  # blank separator between hunks
            out.append(line)
            continue
        if in_hunk and not line.startswith((" ", "+", "-", "\\")):
            out.append("+" + line)
            continue
        out.append(line)
    return "\n".join(out)


class DockerSandbox:
    """One container per task. Created, used, destroyed."""

    def __init__(
        self,
        workspace: Path,
        *,
        image: str = DEFAULT_IMAGE,
        network: bool = False,
        timeout_s: int = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.image = image
        self.network = network
        self.timeout_s = timeout_s
        self.container_name = f"bucker-{uuid.uuid4().hex[:12]}"
        self._started = False

    # ---------------------------------------------------------- lifecycle --
    async def start(self) -> None:
        args = build_run_args(
            container_name=self.container_name,
            workspace=self.workspace,
            image=self.image,
            network=self.network,
        )
        code, out, err = await _run(args, timeout_s=120)
        if code != 0:
            raise SandboxError(f"failed to start container: {err.strip() or out.strip()}")
        self._started = True

    async def stop(self) -> None:
        """Always call this. A leaked container holds memory and a workspace."""
        if not self._started:
            return
        await _run(["docker", "rm", "--force", self.container_name], timeout_s=60)
        self._started = False

    async def __aenter__(self) -> DockerSandbox:
        await self.start()
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.stop()

    # --------------------------------------------------------------- exec --
    async def exec(self, command: str, *, timeout_s: int | None = None) -> ExecResult:
        """Run a shell command inside the container.

        Output is captured verbatim, then secret-scanned before it is returned
        for storage. The unredacted text never leaves this method.
        """
        if not self._started:
            raise SandboxError("sandbox not started")

        timeout = timeout_s or self.timeout_s
        args = ["docker", "exec", self.container_name, "sh", "-c", command]

        loop = asyncio.get_running_loop()
        started = loop.time()
        code, out, err = await _run(args, timeout_s=timeout)
        duration_ms = int((loop.time() - started) * 1000)

        timed_out = code == -1
        safe_out, found_out = redact(out)
        safe_err, found_err = redact(err)

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
    def write_file(self, relative_path: str, content: str) -> Path:
        """Write into the workspace from the host side.

        Path traversal is rejected: a model-authored path must not be able to
        escape the workspace and write to the host filesystem.

        Normalises CRLF to LF: the sandbox is a Linux container, and Windows
        line endings break git apply, pytest, and every tool in the image.
        """
        target = (self.workspace / relative_path).resolve()
        root = self.workspace.resolve()
        # is_relative_to, NOT str.startswith: the string form would accept
        # a sibling like "workspace2/file" as inside "workspace". It is also
        # case-insensitive on Windows, unlike a raw prefix comparison.
        if not target.is_relative_to(root):
            raise SandboxError(f"path escapes workspace: {relative_path!r}")

        target.parent.mkdir(parents=True, exist_ok=True)
        # open(..., newline='') disables universal-newline translation.
        # Without it, Path.write_text converts \n to \r\n on Windows,
        # which breaks every tool in the Linux sandbox (git, python, pytest).
        with open(target, "w", encoding="utf-8", newline="") as f:
            f.write(content.replace("\r\n", "\n"))
        return target

    def read_file(self, relative_path: str) -> str:
        target = (self.workspace / relative_path).resolve()
        root = self.workspace.resolve()
        if not target.is_relative_to(root):
            raise SandboxError(f"path escapes workspace: {relative_path!r}")
        return target.read_text(encoding="utf-8")

    async def apply_diff(
        self, diff: str, *, files: list[str] | None = None
    ) -> ExecResult:
        """Apply a unified diff inside the sandbox, never on the host.

        Normalises CRLF -> LF: Docker volumes on Windows hosts can inject
        carriage returns that break git apply.

        Tolerance switches, all for the same reason — small models write
        sloppy-but-fixable diffs, and the verifier, not the applier, is the
        judge of whether the result is correct:

          * ``ensure_diff_headers``: bare hunks get ``---``/``+++`` headers
            prepended from the worker's ``files_touched``.
          * ``repair_diff_prefixes``: hunk lines that lost their ``+``
            prefix are restored.
          * ``git apply --recount``: hunk headers often carry wrong line
            counts (e.g. ``@@ -1,5 +1,6 @@`` for a 3-line file). git infers
            the counts from the hunk content instead of trusting them.
          * the ``patch`` fallback chain tries ``-p1`` (for ``a/``-prefixed
            paths) then ``-p0`` (for the bare filenames small models emit).
            ``patch`` is also far more tolerant of fuzzy context than
            ``git apply``, which rejects hunks whose context drifted.
        """
        diff = repair_diff_prefixes(ensure_diff_headers(diff, files))
        self.write_file(".bucker.patch", diff.replace("\r\n", "\n"))
        return await self.exec(
            "git apply --recount --verbose .bucker.patch 2>&1 || "
            "patch -p1 --forward < .bucker.patch || "
            "patch -p0 --forward < .bucker.patch"
        )


# ------------------------------------------------------------- subprocess ----
async def _run(args: list[str], *, timeout_s: int) -> tuple[int, str, str]:
    """Run a host command. Returns (code, stdout, stderr); code -1 on timeout."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise SandboxError(
            "docker not found on PATH — the sandbox requires Docker"
        ) from exc

    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "", f"timed out after {timeout_s}s: {shlex.join(args)}"

    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


async def docker_available() -> bool:
    """True when Docker is usable. Tests skip the integration path without it."""
    try:
        code, _, _ = await _run(["docker", "info"], timeout_s=15)
    except SandboxError:
        return False
    return code == 0
