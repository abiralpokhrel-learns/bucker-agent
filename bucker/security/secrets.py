"""Secret scanning — runs before anything reaches storage (step 18).

[HAND] — the event log is append-only and permanent. A credential written into
it cannot be deleted, only tombstoned by a compensating event, and the original
blob still exists. So the scan has to happen *before* the write, not as cleanup
after. There is no undo here.

Scope, honestly stated: this catches known-shaped credentials — AWS keys, API
tokens with recognisable prefixes, private key blocks, obvious assignments. It
will not catch a password that looks like an ordinary English word, and it is
not a substitute for keeping real secrets out of the sandbox in the first
place (they belong in a secrets manager, injected at execution time, never
persisted). Defence in depth, not a guarantee.

False positives are acceptable here. Redacting something harmless costs a line
of noise in a log; missing a live key costs a rotation and an incident.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

REDACTED = "[REDACTED:{kind}]"


@dataclass(frozen=True, slots=True)
class Finding:
    kind: str
    start: int
    end: int
    preview: str          # first few chars only — never the whole secret


# Ordered most-specific first: a match consumes its span, so a precise pattern
# should win over the generic assignment catch-all.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Private keys — the whole PEM block, not just the header.
    ("private_key", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    )),
    # AWS access key id: fixed prefixes, fixed length.
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b")),
    # AWS secret access key, only when explicitly labelled — a bare 40-char
    # base64ish string is far too common to redact on sight.
    ("aws_secret_key", re.compile(
        r"(?i)aws_secret_access_key\s*[=:]\s*['\"]?([A-Za-z0-9/+=]{40})['\"]?"
    )),
    # GitHub tokens.
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    # OpenAI-style and Anthropic-style API keys (prefix-based, not model names).
    ("api_key_sk", re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_\-]{20,}\b")),
    # Slack.
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b")),
    # Google API key.
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    # JSON Web Token.
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b")),
    # Connection strings with inline credentials.
    ("connection_string", re.compile(
        r"\b(?:postgres|postgresql|mysql|mongodb|redis|amqp)(?:\+\w+)?://"
        r"[^:\s/]+:([^@\s]+)@[^\s]+"
    )),
    # Generic labelled assignment: password=..., api_key: "...", token = '...'
    ("generic_secret", re.compile(
        r"(?i)\b(?:password|passwd|secret|api[_\-]?key|auth[_\-]?token|access[_\-]?token"
        r"|private[_\-]?key|client[_\-]?secret)\b\s*[=:]\s*['\"]([^'\"\s]{8,})['\"]"
    )),
]

#: Obvious placeholders that should never be treated as live credentials.
_PLACEHOLDERS = re.compile(
    r"(?i)^(?:x{3,}|\*{3,}|\.{3,}|<[^>]+>|\$\{[^}]+\}|your[_\-]?\w+|example|"
    r"changeme|placeholder|redacted|dummy|fake|test|none|null|todo)$"
)


def _is_placeholder(value: str) -> bool:
    return bool(_PLACEHOLDERS.match(value.strip()))


def scan(text: str) -> list[Finding]:
    """Return every credential-shaped span, earliest first, non-overlapping."""
    if not text:
        return []

    findings: list[Finding] = []
    claimed: list[tuple[int, int]] = []

    for kind, pattern in _PATTERNS:
        for match in pattern.finditer(text):
            # The captured group is the secret when the pattern has one
            # (labelled assignments); otherwise the whole match is the secret.
            if match.groups():
                start, end = match.span(1)
                value = match.group(1)
            else:
                start, end = match.span(0)
                value = match.group(0)

            if _is_placeholder(value):
                continue
            if any(s < end and start < e for s, e in claimed):
                continue

            claimed.append((start, end))
            findings.append(
                Finding(kind=kind, start=start, end=end, preview=value[:4] + "...")
            )

    return sorted(findings, key=lambda f: f.start)


def redact(text: str) -> tuple[str, list[Finding]]:
    """Return (redacted_text, findings). Safe to store."""
    findings = scan(text)
    if not findings:
        return text, []

    out: list[str] = []
    cursor = 0
    for f in findings:
        out.append(text[cursor:f.start])
        out.append(REDACTED.format(kind=f.kind))
        cursor = f.end
    out.append(text[cursor:])

    return "".join(out), findings


def contains_secret(text: str) -> bool:
    return bool(scan(text))
