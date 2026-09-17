from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any


class GitError(Exception):
    """Exception raised for git command failures."""

    def __init__(self, message: str, returncode: int):
        super().__init__(f"{message} (exit code: {returncode})")
        self.returncode = returncode


async def _run_git(root: Path, *args: str) -> str:
    """Run a git command and return its stdout."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(root),
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            err_msg = stderr.decode().strip() or stdout.decode().strip()
            raise GitError(
                f"Git command failed: git {' '.join(args)}\nError: {err_msg}", proc.returncode
            )

        return stdout.decode().strip()
    except FileNotFoundError as exc:
        raise GitError("git executable not found in PATH", -1) from exc


async def is_git_repo(root: Path) -> bool:
    """Check if the given directory is a git repository."""
    try:
        await _run_git(root, "rev-parse", "--is-inside-work-tree")
        return True
    except GitError:
        return False


async def git_status(root: Path) -> dict[str, Any]:
    """Get the current git status as a parsed dictionary."""
    try:
        # Get branch and tracking info
        branch_out = await _run_git(root, "status", "-b", "--porcelain=v1")
        lines = branch_out.splitlines()

        branch = "unknown"
        ahead = 0
        behind = 0

        if lines and lines[0].startswith("##"):
            branch_info = lines[0][3:].strip()
            if "..." in branch_info:
                branch = branch_info.split("...")[0]
                if "[ahead " in branch_info:
                    ahead_str = branch_info.split("[ahead ")[1].split("]")[0].split(",")[0]
                    ahead = int(ahead_str)
                if "behind " in branch_info:
                    behind_str = branch_info.split("behind ")[1].split("]")[0]
                    behind = int(behind_str)
            else:
                branch = branch_info

        # Get detailed status
        status_out = await _run_git(root, "status", "--porcelain=v1")
        staged = []
        modified = []
        untracked = []

        for line in status_out.splitlines():
            if not line or line.startswith("##"):
                continue

            xy = line[0:2]
            path = line[3:]

            if xy == "??":
                untracked.append(path)
            else:
                if xy[0] != " " and xy[0] != "?":
                    staged.append(path)
                if xy[1] != " " and xy[1] != "?":
                    modified.append(path)

        return {
            "branch": branch,
            "clean": len(staged) == 0 and len(modified) == 0 and len(untracked) == 0,
            "staged": staged,
            "modified": modified,
            "untracked": untracked,
            "ahead": ahead,
            "behind": behind,
        }
    except GitError as e:
        raise e


async def git_log(root: Path, n: int = 20) -> list[dict[str, str]]:
    """Get the git commit log."""
    try:
        # Format: hash|short_hash|author|date|message
        log_format = "%H|%h|%an|%aI|%s"
        out = await _run_git(root, "log", f"-n{n}", f"--format={log_format}")

        logs = []
        for line in out.splitlines():
            if not line:
                continue
            parts = line.split("|", 4)
            if len(parts) == 5:
                logs.append(
                    {
                        "hash": parts[0],
                        "short_hash": parts[1],
                        "author": parts[2],
                        "date": parts[3],
                        "message": parts[4],
                    }
                )
        return logs
    except GitError:
        return []


async def git_diff(root: Path, staged: bool = False) -> str:
    """Get the git diff."""
    args = ["diff"]
    if staged:
        args.append("--staged")
    return await _run_git(root, *args)


async def git_stash(root: Path, message: str = "") -> bool:
    """Stash current changes."""
    args = ["stash"]
    if message:
        args.extend(["push", "-m", message])
    try:
        await _run_git(root, *args)
        return True
    except GitError:
        return False


async def git_stash_pop(root: Path) -> bool:
    """Pop the most recent stash."""
    try:
        await _run_git(root, "stash", "pop")
        return True
    except GitError:
        return False


async def git_commit(root: Path, message: str, paths: list[str] | None = None) -> str:
    """Commit changes."""
    if paths:
        await _run_git(root, "add", *paths)

    await _run_git(root, "commit", "-m", message)
    # Return the new commit hash
    return await _run_git(root, "rev-parse", "HEAD")


async def git_branch(root: Path, name: str) -> bool:
    """Create and checkout a new branch."""
    try:
        await _run_git(root, "checkout", "-b", name)
        return True
    except GitError:
        return False


async def git_current_branch(root: Path) -> str:
    """Get the current branch name."""
    try:
        return await _run_git(root, "rev-parse", "--abbrev-ref", "HEAD")
    except GitError:
        return ""
