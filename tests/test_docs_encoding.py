"""Docs encoding hygiene (review feedback: 'docs have mojibake').

The project's docs must stay valid UTF-8 with no double-encoded characters.
The mojibake the review quoted (``â€"`` for an em dash, ``â†'`` for an arrow,
``Ï‡Â²`` for chi-squared) is what a UTF-8 file *looks like* when read with a
wrong code page — it is not in the files, and this test is the tripwire that
keeps it out.

Scans every project file (excluding .venv/.git) for:
  1. files that fail to decode as UTF-8 at all;
  2. the exact byte signatures of CP1252-double-encoded UTF-8 sequences.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Byte signatures of classic double-encoded UTF-8 (decoded as CP1252, then
#: re-saved as UTF-8). Each is the mojibake of one real character:
#:   C3 A2 E2 82 AC ...  -> â€..  (em/en dash, curly quotes)
#:   C3 A2 E2 80 A0      -> â†    (arrow)
#:   C3 8F E2 80 A1      -> Ï‡    (chi)
#:   C3 82 C2 B2         -> Â²    (superscript two)
#:   C3 83 C2 A9         -> Ã©    (e-acute)
#:   C3 82 C2 B7         -> Â·    (middle dot)
#:   C3 A2 E2 80 A6      -> â€¦   (ellipsis)
_MOJIBAKE_SIGNATURES = (
    rb"\xc3\xa2\xe2\x82\xac",
    rb"\xc3\xa2\xe2\x80\xa0",
    rb"\xc3\x8f\xe2\x80\xa1",
    rb"\xc3\x82\xc2\xb2",
    rb"\xc3\x83\xc2\xa9",
    rb"\xc3\x82\xc2\xb7",
    rb"\xc3\xa2\xe2\x80\xa6",
)

#: File types that carry prose. Binary/build dirs are skipped.
_SUFFIXES = {".md", ".txt", ".py", ".toml", ".sql", ".yml", ".yaml", ".example", ".cfg"}


def _project_files() -> list[Path]:
    files = []
    for p in ROOT.rglob("*"):
        if not p.is_file() or p.suffix not in _SUFFIXES:
            continue
        if any(part in {".venv", ".git", "__pycache__", ".pytest_cache", ".ruff_cache"} for part in p.parts):
            continue
        files.append(p)
    return files


def test_all_prose_files_decode_as_utf8() -> None:
    bad = []
    for p in _project_files():
        try:
            p.read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            bad.append(f"{p.relative_to(ROOT)}: {exc}")
    assert not bad, f"files are not valid UTF-8: {bad}"


def test_no_mojibake_signatures_in_project_files() -> None:
    # tests/ is excluded: source code can legitimately contain byte-sequence
    # literals (including this file's own signature definitions); the prose
    # the review complained about lives outside tests/.
    offenders = []
    for p in _project_files():
        if "tests" in p.parts:
            continue
        data = p.read_bytes()
        for sig in _MOJIBAKE_SIGNATURES:
            if re.search(sig, data):
                offenders.append(f"{p.relative_to(ROOT)} (signature {sig!r})")
                break
    assert not offenders, (
        "double-encoded (mojibake) characters found — re-save these files "
        f"as UTF-8: {offenders}"
    )
