"""SWE-bench integration (BUILD_PLAN step 26).

Loads SWE-bench Lite instances, seeds sandbox workspaces with the repo at
the base commit, and runs the official evaluation harness.

swebench is imported lazily — its initialization downloads models and data,
so keeping it out of module scope keeps imports fast and tests isolated.
"""

from __future__ import annotations

import json
import subprocess
import sys
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
    """Download SWE-bench Lite from Hugging Face and convert to JSON.

    The old raw-GitHub URL is gone; the canonical copy now lives at
    princeton-nlp/SWE-bench_Lite. The dev split ships as parquet, so we
    convert to the flat JSON list this module expects. On Windows the
    conversion runs inside the WSL harness venv (which has pandas).
    """
    import urllib.request

    url = (
        "https://huggingface.co/datasets/princeton-nlp/SWE-bench_Lite/"
        "resolve/main/data/test-00000-of-00001.parquet"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    parquet = target.with_suffix(".parquet")
    tmp = target.with_suffix(".tmp")
    try:
        urllib.request.urlretrieve(url, str(parquet))
    except Exception as exc:
        if parquet.exists():
            parquet.unlink()
        raise SWEBenchError(
            f"Failed to download SWE-bench Lite from {url}: {exc}"
        ) from exc

    convert_snippet = (
        "import sys, json, pandas as pd;"
        "df = pd.read_parquet(sys.argv[1]);"
        "df.to_json(sys.argv[2], orient='records', lines=False)"
    )
    if sys.platform == "win32":
        cmd = [
            "wsl", "-d", WSL_DISTRO, "-u", "root", "-e",
            WSL_HARNESS_PYTHON, "-c", convert_snippet,
            _win_to_wsl_path(parquet), _win_to_wsl_path(tmp),
        ]
    else:
        cmd = [sys.executable, "-c", convert_snippet, str(parquet), str(tmp)]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0 or not tmp.exists():
        if parquet.exists():
            parquet.unlink()
        if tmp.exists():
            tmp.unlink()
        raise SWEBenchError(
            f"Failed to convert SWE-bench Lite parquet to JSON:\n"
            f"{result.stderr[-500:]}"
        )
    tmp.replace(target)
    parquet.unlink()


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
    cache_level: str | None = None,
) -> list[dict]:
    """Run the official SWE-bench evaluation harness.

    ``predictions_path`` must be a JSON file with the prediction format.
    ``instances_path`` is the dataset used for ground truth.

    This calls the official harness via subprocess so the swebench package
    (which is heavy to import) is only loaded at evaluation time.
    ``cache_level`` is accepted for backward compatibility but ignored —
    swebench 5.x removed the flag.

    Note: swebench's harness grades against the *test split* of its dataset
    argument; we pass our downloaded test-split JSON directly.
    """
    instances_path = Path(instances_path or DATASET_PATH)

    if not instances_path.exists():
        raise SWEBenchError(
            f"instances file not found at {instances_path}. "
            f"Download it first with load_instances()."
        )

    # The official harness is POSIX-only (it imports `resource`), so on native
    # Windows we run it inside WSL2 (Ubuntu-24.04) via a prepared venv at
    # /root/swebench-venv. Windows paths are translated to /mnt/c/... so the
    # harness in the VM reads the same files the host wrote.
    if sys.platform == "win32":
        return _run_evaluation_wsl(predictions_path, instances_path, max_workers, cache_level)

    cmd = [
        sys.executable, "-m", "swebench.harness.run_evaluation",
        "--dataset_name", str(instances_path),
        "--predictions_path", str(predictions_path),
        "--max_workers", str(max_workers),
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


#: WSL distro + venv used for the official harness on native Windows.
WSL_DISTRO = "Ubuntu-24.04"
WSL_HARNESS_PYTHON = "/root/swebench-venv/bin/python"


def _win_to_wsl_path(p: Path) -> str:
    """Translate a Windows path to its /mnt/<drive>/... WSL equivalent."""
    resolved = str(Path(p).resolve())
    drive = resolved[0].lower()
    rest = resolved[2:].replace("\\", "/")
    return f"/mnt/{drive}{rest}"


def _run_evaluation_wsl(
    predictions_path: Path,
    instances_path: Path,
    max_workers: int,
    cache_level: str,
) -> list[dict]:
    """Run the official harness inside WSL2 (Windows-only path)."""
    wsl_preds = _win_to_wsl_path(predictions_path)
    wsl_instances = _win_to_wsl_path(instances_path)

    cmd = [
        "wsl", "-d", WSL_DISTRO, "-u", "root", "-e",
        WSL_HARNESS_PYTHON, "-m", "swebench.harness.run_evaluation",
        "--dataset_name", wsl_instances,
        "--predictions_path", wsl_preds,
        "--max_workers", str(max_workers),
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
            f"WSL not available ({exc}). Install Ubuntu-24.04 and the harness "
            f"venv: see docs/WSL2_SETUP.md"
        ) from exc

    if result.returncode != 0:
        raise SWEBenchError(
            f"evaluation failed (exit {result.returncode}):\n"
            f"stderr: {result.stderr[-1000:]}"
        )

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
