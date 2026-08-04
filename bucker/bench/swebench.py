"""SWE-bench integration (BUILD_PLAN step 26).

Loads SWE-bench Lite instances, seeds sandbox workspaces with the repo at
the base commit, and runs the official evaluation harness.

swebench is imported lazily — its initialization downloads models and data,
so keeping it out of module scope keeps imports fast and tests isolated.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from bucker.config import settings

#: Where SWE-bench Lite JSON lives. Downloaded on first use.
DATASET_PATH = Path(settings.blob_root).parent / "swebench_lite.json"


class SWEBenchError(Exception):
    """Something went wrong with SWE-bench — download, checkout, or evaluation."""


@dataclass(frozen=True, slots=True)
class SWEInstance:
    """One SWE-bench Lite instance, parsed from the JSON dataset."""

    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    test_patch: str
    hints_text: str = ""
    version: str = ""
    fail_to_pass: list[str] = ()
    pass_to_pass: list[str] = ()

    @classmethod
    def from_dict(cls, d: dict) -> SWEInstance:
        return cls(
            instance_id=d["instance_id"],
            repo=d["repo"],
            base_commit=d["base_commit"],
            problem_statement=d.get("problem_statement", d.get("issue", "")),
            test_patch=d.get("test_patch", ""),
            hints_text=d.get("hints_text", ""),
            version=d.get("version", ""),
            fail_to_pass=d.get("FAIL_TO_PASS", d.get("fail_to_pass", [])),
            pass_to_pass=d.get("PASS_TO_PASS", d.get("pass_to_pass", [])),
        )


# ------------------------------------------------------------ dataset load ---


def load_instances(path: Path | None = None) -> list[SWEInstance]:
    """Load SWE-bench Lite instances from a JSON file.

    If the file doesn't exist, attempts to download it from the official
    repository.
    """
    path = Path(path or DATASET_PATH)

    if not path.exists():
        _download_dataset(path)

    raw = json.loads(path.read_text(encoding="utf-8"))
    return [SWEInstance.from_dict(d) for d in raw]


def _download_dataset(target: Path) -> None:
    """Download SWE-bench Lite from the official repository."""
    import urllib.request

    url = (
        "https://raw.githubusercontent.com/princeton-nlp/SWE-bench/main/"
        "swebench/harness/data/swe-bench-lite.json"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    try:
        urllib.request.urlretrieve(url, str(tmp))
        tmp.replace(target)
    except Exception as exc:
        if tmp.exists():
            tmp.unlink()
        raise SWEBenchError(
            f"Failed to download SWE-bench Lite dataset from {url}: {exc}"
        ) from exc


# ----------------------------------------------------------- workspace seed ---


def _run(cmd: list[str], *, timeout_s: int = 300, cwd: Path | None = None) -> str:
    """Run a command, return stdout. Raises SWEBenchError on failure."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd=str(cwd) if cwd else None,
        )
    except subprocess.TimeoutExpired as exc:
        raise SWEBenchError(f"timed out after {timeout_s}s: {' '.join(cmd)}") from exc
    except FileNotFoundError as exc:
        raise SWEBenchError(
            f"command not found: {cmd[0]}. Is git installed?"
        ) from exc

    if result.returncode != 0:
        raise SWEBenchError(
            f"command failed (exit {result.returncode}): {' '.join(cmd)}\n"
            f"stderr: {result.stderr[:500]}"
        )
    return result.stdout.strip()


def clone_instance(instance: SWEInstance, workspace: Path) -> None:
    """Clone the instance's repo at its base commit into the workspace.

    Runs on the host, before the sandbox container is created. The sandbox
    then mounts this workspace — the repo is already there, no network needed.
    """
    if workspace.exists():
        # If the workspace already has the repo, check it's the right one.
        existing = subprocess.run(
            ["git", "-C", str(workspace), "log", "-1", "--format=%H"],
            capture_output=True, text=True, timeout=30,
        )
        if existing.returncode == 0 and existing.stdout.strip() == instance.base_commit:
            return  # already at the right commit

    workspace.mkdir(parents=True, exist_ok=True)

    # Shallow clone: just the one commit we need. SWE-bench repos can be large.
    repo_url = f"https://github.com/{instance.repo}.git"
    _run(
        ["git", "clone", "--depth", "1", repo_url, str(workspace)],
        timeout_s=600,
    )
    _run(
        ["git", "-C", str(workspace), "fetch", "--depth", "1", "origin",
         instance.base_commit],
        timeout_s=300,
    )
    _run(
        ["git", "-C", str(workspace), "checkout", instance.base_commit],
        timeout_s=120,
    )


# ----------------------------------------------------------- patch extraction --


def prediction_from_diff(diff: str, instance_id: str, model_name: str = "") -> dict:
    """Build a SWE-bench prediction dict from a unified diff.

    This is the format the official harness expects.
    """
    return {
        "instance_id": instance_id,
        "model_patch": diff,
        "model_name_or_path": model_name or "bucker-agent",
    }


# ---------------------------------------------------- official evaluation ----


def run_evaluation(
    predictions_path: Path,
    *,
    instances_path: Path | None = None,
    max_workers: int = 4,
    cache_level: str = "none",
) -> list[dict]:
    """Run the official SWE-bench evaluation harness.

    ``predictions_path`` must be a JSON file with the prediction format.
    ``instances_path`` is the dataset used for ground truth.

    This calls the official harness via subprocess so the swebench package
    (which is heavy to import) is only loaded at evaluation time.
    """
    instances_path = Path(instances_path or DATASET_PATH)

    if not instances_path.exists():
        raise SWEBenchError(
            f"instances file not found at {instances_path}. "
            f"Download it first with load_instances()."
        )

    cmd = [
        "python", "-m", "swebench.harness.run_evaluation",
        "--dataset_name", str(instances_path),
        "--predictions_path", str(predictions_path),
        "--max_workers", str(max_workers),
        "--cache_level", cache_level,
        "--run_id", "bucker-eval",
    ]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=3600,
        )
    except subprocess.TimeoutExpired as exc:
        raise SWEBenchError("evaluation timed out after 1 hour") from exc
    except FileNotFoundError as exc:
        raise SWEBenchError(
            "swebench harness not found. Run: uv sync --extra dev"
        ) from exc

    if result.returncode != 0:
        raise SWEBenchError(
            f"evaluation failed (exit {result.returncode}):\n"
            f"stderr: {result.stderr[:1000]}"
        )

    # The harness writes results to a predictions file with .report.json suffix.
    report_path = predictions_path.with_suffix(".report.json")
    if not report_path.exists():
        raise SWEBenchError(
            f"evaluation completed but no report found at {report_path}"
        )

    return json.loads(report_path.read_text(encoding="utf-8"))


# -------------------------------------------------------------- convenience --


def first_n_instances(n: int = 5, path: Path | None = None) -> list[SWEInstance]:
    """Load the first N instances. For smoke testing the pipeline."""
    return load_instances(path)[:n]
