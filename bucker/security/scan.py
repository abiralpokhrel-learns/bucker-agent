from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

# Comprehensive regex patterns
SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("AWS Access Key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("AWS Secret Key", re.compile(r"(?i)aws.+secret.+[A-Za-z0-9/+=]{40}")),
    ("GitHub Token", re.compile(r"gh[ps]_[A-Za-z0-9_]{36,}")),
    ("GitLab Token", re.compile(r"glpat-[A-Za-z0-9_-]{20,}")),
    ("Google API Key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("Stripe Key", re.compile(r"[sr]k_(?:live|test)_[A-Za-z0-9]{24,}")),
    ("Slack Token", re.compile(r"xox[bpars]-[A-Za-z0-9-]{10,}")),
    (
        "Generic High Entropy",
        re.compile(
            r"(?i)(?:key|secret|token|password)\s*[:=]\s*['\"]?([A-Za-z0-9/+=]{40,})['\"]?"
        ),
    ),
]


@dataclass(slots=True)
class SecretFinding:
    pattern_name: str
    line: int
    column: int
    snippet: str
    confidence: float


def get_allowlist() -> set[str]:
    env_val = os.getenv("BUCKER_SECRET_ALLOWLIST", "")
    return {p.strip() for p in env_val.split(",") if p.strip()}


def scan_text(text: str) -> list[SecretFinding]:
    findings = []
    allowlist = get_allowlist()
    lines = text.splitlines()

    for line_idx, line in enumerate(lines):
        for pattern_name, regex in SECRET_PATTERNS:
            if pattern_name in allowlist:
                continue
            for match in regex.finditer(line):
                findings.append(
                    SecretFinding(
                        pattern_name=pattern_name,
                        line=line_idx + 1,
                        column=match.start() + 1,
                        snippet=line[max(0, match.start() - 20) : match.end() + 20].strip(),
                        confidence=0.9,
                    )
                )
    return findings


def scan_file(path: Path) -> list[SecretFinding]:
    try:
        content = path.read_text(encoding="utf-8")
        return scan_text(content)
    except Exception:
        return []


def scan_directory(root: Path, *, exclude: list[str]) -> list[SecretFinding]:
    findings = []
    for filepath in root.rglob("*"):
        if filepath.is_file():
            skip = False
            for ex in exclude:
                if ex in str(filepath):
                    skip = True
                    break
            if not skip:
                findings.extend(scan_file(filepath))
    return findings


def redact(text: str) -> str:
    allowlist = get_allowlist()
    redacted_text = text
    for pattern_name, regex in SECRET_PATTERNS:
        if pattern_name in allowlist:
            continue
        redacted_text = regex.sub("[REDACTED]", redacted_text)
    return redacted_text
