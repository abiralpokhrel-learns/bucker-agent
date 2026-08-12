"""bucker doctor — diagnose a broken local setup and fail cleanly.

Usage:
    uv run python -m scripts.doctor

Every check is independent and reported as [OK] / [FAIL] / [WARN] with an
actionable hint. The exit code is 0 only when the essentials are healthy
(python, venv, imports, config) — infra services (Docker, Postgres,
Temporal, Ollama) are reported but do not fail the run, because you can
still develop against recorded mode without them.

Checks, in order:
  1. uv present and the project's venv is loadable
  2. the venv's base interpreter (pyvenv.cfg home) actually exists — the
     "python.exe points to a missing interpreter" failure mode
  3. core dependencies import (fastapi, pydantic, asyncpg; litellm is
     optional and reported separately)
  4. .env exists and was loaded by bucker.config
  5. Docker is usable and the sandbox image exists
  6. Postgres answers SELECT 1 (as the configured app role)
  7. Temporal is reachable
  8. Ollama answers on its API, if a configured model uses it
  9. the configured model chain is non-empty and documented
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ESSENTIAL_FAILED = False
#: Set by check_uv_and_venv: whether .venv python actually executes. When
#: False, every check that spawns the venv python is skipped — the doctor
#: must still work when the thing it diagnoses is a dead venv.
VENV_OK = False


def _venv_layout() -> tuple[str, str]:
    """(scripts_dir, python_exe) for this platform, as plain strings.

    Windows venvs put the interpreter at ``.venv/Scripts/python.exe``;
    POSIX (Linux/macOS/WSL2) at ``.venv/bin/python``. Hardcoding either
    layout makes doctor lie on the other platform — the whole point of
    this tool is to report the truth about the local setup. Kept as a
    pure string tuple so the platform logic is unit-testable without
    constructing platform-flavoured paths.
    """
    if os.name == "nt":
        return "Scripts", "python.exe"
    return "bin", "python"


def venv_python() -> Path:
    """The project venv's interpreter, for this platform."""
    scripts, exe = _venv_layout()
    return ROOT / ".venv" / scripts / exe


def base_interpreter(home: str) -> Path:
    """The base interpreter named by pyvenv.cfg, for this platform."""
    _, exe = _venv_layout()
    return Path(home) / exe


def report(kind: str, label: str, detail: str = "") -> None:
    global ESSENTIAL_FAILED
    icon = {"OK": "[OK]  ", "FAIL": "[FAIL]", "WARN": "[WARN]"}[kind]
    print(f"{icon} {label}")
    if detail:
        print(f"      {detail}")
    if kind == "FAIL":
        ESSENTIAL_FAILED = True


def _run(cmd: list[str], timeout: float = 15) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", "command not found")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "timed out")


def check_uv_and_venv() -> None:
    uv = shutil.which("uv")
    if not uv:
        report("FAIL", "uv is not on PATH",
               "install it: https://docs.astral.sh/uv/ (winget install astral-sh.uv)")
        return
    report("OK", f"uv found: {uv}")

    if not shutil.which("python"):
        report("WARN", "python is not on PATH",
               "harmless if you use uv, but add the interpreter dir (e.g. "
               "C:\\Users\\<you>\\AppData\\Local\\Python\\pythoncore-3.14-64) "
               "to PATH for plain `python`/`pip` usage")

    venv_py = venv_python()
    if not venv_py.exists():
        report("FAIL", ".venv is missing", "run: uv sync --extra dev")
        return
    cfg = ROOT / ".venv" / "pyvenv.cfg"
    home = ""
    if cfg.exists():
        for line in cfg.read_text(encoding="utf-8").splitlines():
            if line.startswith("home ="):
                home = line.split("=", 1)[1].strip()
    base = base_interpreter(home) if home else None
    if not base or not base.exists():
        report("FAIL", "venv base interpreter is missing",
               f"pyvenv.cfg home = {home or '(unset)'} — "
               "reinstall python or run: uv venv --clear && uv sync")
        return
    report("OK", f"venv base interpreter: {base}")

    probe = _run([str(venv_py), "-c", "import sys; print(sys.version.split()[0])"])
    global VENV_OK
    if probe.returncode != 0:
        VENV_OK = False
        hint = "run: uv venv --clear && uv sync"
        if "trampoline" in probe.stderr.lower():
            # uv's .venv/Scripts/python.exe is a small launcher that spawns
            # the base interpreter; Windows (Smart App Control / ACLs) can
            # block that spawn. A venv built by the real interpreter has a
            # real python.exe and no launcher at all.
            hint = (
                "the venv launcher cannot spawn its base interpreter — "
                'rebuild with the real python: python -m venv --clear .venv '
                "&& uv sync"
            )
        elif "Access is denied" in probe.stderr or "Unable to create process" in probe.stderr:
            # The python.exe itself is blocked (ACL / Smart App Control) or
            # its base interpreter directory is unreachable. Rebuild the venv
            # from a python.org install that lives outside the locked dirs.
            hint = (
                "the venv interpreter is blocked or its base directory is "
                "unreachable — install a python.org release and rebuild: "
                "winget install Python.Python.3.12 && "
                'python -m venv --clear .venv && uv sync'
            )
        report("FAIL", "venv python does not execute",
               probe.stderr.strip()[:200] + " — " + hint)
    else:
        VENV_OK = True
        report("OK", f"venv python executes (CPython {probe.stdout.strip()})")


def check_imports() -> None:
    venv_py = venv_python()
    probe = _run([
        str(venv_py), "-c",
        "import fastapi, pydantic, asyncpg; print('core imports ok')",
    ])
    if probe.returncode != 0:
        report("FAIL", "core dependencies do not import",
               "run: uv sync --extra dev"
               + (f" — {probe.stderr.strip()[:160]}" if probe.stderr else ""))
    else:
        report("OK", "core imports (fastapi, pydantic, asyncpg)")

    probe = _run([str(venv_py), "-c", "import litellm; print('ok')"])
    if probe.returncode == 0:
        report("OK", "litellm installed (live model calls available)")
    else:
        report("WARN", "litellm not installed",
               "live runs need it: uv sync --extra llm (recorded mode works without it)")


def check_config() -> None:
    venv_py = venv_python()
    probe = _run([
        str(venv_py), "-c",
        "from bucker.config import DOTENV_LOADED, DOTENV_ERROR, settings; "
        "print(f'loaded={DOTENV_LOADED} err={DOTENV_ERROR} model={settings.model} mode={settings.model_mode}')",
    ], timeout=30)
    if probe.returncode != 0:
        report("FAIL", "bucker.config does not import",
               probe.stderr.strip()[:200])
        return
    out = probe.stdout.strip()
    report("OK", f"config loads ({out})")
    if "loaded=False" in out:
        report("WARN", ".env missing or unreadable",
               "copy .env.example to .env and adjust")
        return
    # Review #4: the API auth token must not be the dev default in prod-ish
    # setups. Local-only development is fine; say so loudly otherwise.
    from bucker.config import settings as s

    if getattr(s, "api_token", "dev-token") == "dev-token":
        report("WARN", "BUCKER_API_TOKEN is the dev default ('dev-token')",
               "set a real token before exposing the API beyond localhost")

    # Hardening review #10: the uv venv trap. A uv-managed interpreter in
    # AppData\\Roaming\\uv\\python gets ACL-locked on Windows and the venv
    # trampoline cannot spawn it; the fix is a per-user interpreter + this
    # env var pointing at Local. Diagnose the failure mode directly.
    uv_install = os.environ.get("UV_PYTHON_INSTALL_DIR", "")
    if not uv_install:
        report("WARN", "UV_PYTHON_INSTALL_DIR is not set",
               "set it to your LOCAL uv python dir (e.g. "
               "%LOCALAPPDATA%\\uv\\python); the Roaming default gets "
               "ACL-locked and `uv run` breaks with os error 5")
    elif "Roaming" in uv_install:
        report("WARN", "UV_PYTHON_INSTALL_DIR points at Roaming",
               f"({uv_install}) — this is the ACL-locked path that breaks "
               "uv's venv trampoline; move to %LOCALAPPDATA%\\uv\\python")


def check_docker() -> None:
    info = _run(["docker", "info"], timeout=20)
    if info.returncode != 0:
        report("WARN", "Docker is not usable", "start Docker Desktop (sandbox runs need it)")
        return
    report("OK", "Docker is usable")
    img = _run(["docker", "image", "inspect", "bucker-sandbox:latest"], timeout=20)
    if img.returncode == 0:
        report("OK", "sandbox image bucker-sandbox:latest present")
    else:
        report("WARN", "sandbox image missing",
               "build it once: docker build -f Dockerfile.sandbox -t bucker-sandbox:latest .")


def check_postgres() -> None:
    venv_py = venv_python()
    # NOTE: real newlines, not ';' — 'async def' is a compound statement and
    # cannot follow a semicolon on the same line (SyntaxError).
    probe = _run([
        str(venv_py), "-c",
        "import asyncio, asyncpg\n"
        "from bucker.config import settings\n"
        "async def m():\n"
        "    try:\n"
        "        p = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=1, timeout=3)\n"
        "        v = await p.fetchval('SELECT 1')\n"
        "        await p.close()\n"
        "        print('ok' if v == 1 else 'bad')\n"
        "    except Exception as e:\n"
        "        print(f'fail: {type(e).__name__}: {str(e)[:100]}')\n"
        "asyncio.run(m())",
    ], timeout=30)
    out = probe.stdout.strip()
    if out == "ok":
        report("OK", "Postgres answers SELECT 1")
    else:
        report("WARN", f"Postgres unreachable ({out or probe.stderr.strip()[:80]})",
               "start it: docker compose up -d, then: uv run python -m bucker.cli migrate")


def check_temporal() -> None:
    venv_py = venv_python()
    probe = _run([
        str(venv_py), "-c",
        "import asyncio\n"
        "from temporalio.client import Client\n"
        "from bucker.config import settings\n"
        "async def m():\n"
        "    try:\n"
        "        await asyncio.wait_for(Client.connect(settings.temporal_host, namespace=settings.temporal_namespace), timeout=3)\n"
        "        print('ok')\n"
        "    except Exception as e:\n"
        "        print(f'fail: {type(e).__name__}')\n"
        "asyncio.run(m())",
    ], timeout=20)
    if probe.stdout.strip() == "ok":
        report("OK", "Temporal reachable")
    else:
        report("WARN", "Temporal not reachable",
               "start it: temporal server start-dev (workflows need it; recorded tasks still work)")


def check_ollama() -> None:
    venv_py = venv_python()
    probe = _run([
        str(venv_py), "-c",
        "import asyncio; "
        "from bucker.config import settings; "
        "chain = [settings.model, *settings.model_fallbacks]; "
        "print('ollama' if any(m.startswith('ollama/') for m in chain) else 'none')",
    ], timeout=20)
    if probe.stdout.strip() != "ollama":
        report("OK", "no Ollama model configured (nothing to check)")
        return
    import urllib.request

    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=3) as resp:  # noqa: S310
            ok = resp.status == 200
    except Exception as exc:
        ok, detail = False, f"{type(exc).__name__}: {str(exc)[:80]}"
    report("OK" if ok else "WARN", "Ollama answers on 127.0.0.1:11434",
           "" if ok else f"start it: ollama serve ({detail})")


def check_model_chain() -> None:
    venv_py = venv_python()
    probe = _run([
        str(venv_py), "-c",
        "from bucker.config import settings; "
        "print(settings.model, '|', ','.join(settings.model_fallbacks))",
    ], timeout=20)
    chain = probe.stdout.strip()
    if not chain or chain.startswith("|"):
        report("WARN", "no model configured", "set BUCKER_MODEL in .env")
    else:
        report("OK", f"model chain: {chain}")


def main() -> int:
    print(f"bucker doctor — {ROOT}")
    print("(bootstrap mode: run this with ANY working python — `python "
          "scripts/doctor.py` — the checks themselves use stdlib only)\n")
    check_uv_and_venv()
    if VENV_OK:
        check_imports()
        check_config()
        check_model_chain()
    else:
        print("[SKIP] import / config / model-chain checks — they need the "
              "venv python, which is broken above. Fix the [FAIL] item "
              "first, then re-run.\n")
    print()
    if VENV_OK:
        check_docker()
        check_postgres()
        check_temporal()
        check_ollama()
    else:
        print("[SKIP] infra checks (docker/postgres/temporal/ollama) — they "
              "spawn the venv python; re-run after the venv is fixed.\n")
    print()
    if ESSENTIAL_FAILED:
        print("ESSENTIALS BROKEN — fix the [FAIL] items above, then re-run.")
        return 1
    print("Essentials healthy. Infra warnings above are the only gaps.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
