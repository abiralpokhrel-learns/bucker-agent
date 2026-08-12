"""Regression tests for scripts/doctor.py.

The doctor must report the truth about the local setup on EVERY platform.
Bug fixed here: the venv checks hardcoded the Windows layout
(.venv/Scripts/python.exe), so on Linux/macOS/WSL2 doctor reported
".venv is missing" even after a correct `uv sync`, and skipped every
downstream check. The venv layout and the pyvenv.cfg base-interpreter
name are now platform-aware.

Note on testing: the platform logic lives in `_venv_layout()`, a pure
string tuple. We test THAT rather than `venv_python()`/`base_interpreter()`
under a faked os.name — those construct pathlib.Path objects, whose
flavour is decided by the real os.name at construction time, so faking
it from a test raises NotImplementedError on the wrong host.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location("doctor", ROOT / "scripts" / "doctor.py")
assert _spec is not None and _spec.loader is not None
doctor = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(doctor)


@pytest.fixture
def fake_os_name(monkeypatch):
    """Point doctor's os.name at a fake value for the duration of a test."""

    def _set(name: str):
        monkeypatch.setattr(doctor.os, "name", name)

    return _set


def test_venv_layout_windows(fake_os_name):
    fake_os_name("nt")
    assert doctor._venv_layout() == ("Scripts", "python.exe")


def test_venv_layout_posix(fake_os_name):
    fake_os_name("posix")
    assert doctor._venv_layout() == ("bin", "python")


def test_venv_python_windows_layout(fake_os_name):
    fake_os_name("nt")
    assert doctor.venv_python() == ROOT / ".venv" / "Scripts" / "python.exe"


def test_venv_python_posix_layout(fake_os_name):
    fake_os_name("posix")
    assert doctor.venv_python() == ROOT / ".venv" / "bin" / "python"


def test_base_interpreter_appends_platform_exe(tmp_path):
    """The check reads `home` from pyvenv.cfg and appends the right exe name.

    Uses the REAL platform layout (no os.name faking) so pathlib and
    doctor agree on the path flavour.
    """
    _, exe = doctor._venv_layout()
    assert doctor.base_interpreter(str(tmp_path)) == tmp_path / exe


def test_doctor_imports_with_stdlib_only():
    """doctor is a bootstrap tool: it must import with NO third-party deps.

    Runs on a broken setup by definition — if it needed fastapi/asyncpg/
    temporalio to even load, it could never diagnose a missing venv.
    """
    imported = {n.split(".")[0] for n in doctor.__dict__ if n in sys.modules}
    third_party = imported - {
        "os", "shutil", "subprocess", "sys", "pathlib", "doctor",
    }
    assert not third_party, f"doctor imports non-stdlib modules: {sorted(third_party)}"
