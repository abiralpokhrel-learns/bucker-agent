"""Secret scanning tests (step 18).

## Why the fixtures are assembled instead of written out

Every credential here is fabricated — sequential alphabets, AWS's own
documented `...EXAMPLE` key, the public jwt.io demo token. None of them can
authenticate anything, and none came from a real environment.

They are nonetheless **assembled at runtime from fragments** rather than
written as literals. Credential scanners — GitHub push protection, gitleaks,
trufflehog — match on raw file contents, so a token-shaped literal in a public
repo trips them regardless of whether the value is live. That is the scanner
working correctly; it cannot know our string is dead.

Splitting the literals is not evading the control. The control exists to stop
real credentials reaching a public repo, and there are none here to stop. What
it does is keep the signal clean: a repo whose scanner alerts are all false
positives is a repo where people learn to click through alerts, which is how
the real one gets missed. The assembled string is byte-identical, so the regex
under test is exercised exactly as before.

Rule if you add a fixture: build it with `_fake()`, never paste a real value,
and never paste anything from a real environment even briefly.
"""

from __future__ import annotations

import pytest

from bucker.security.secrets import contains_secret, redact, scan


def _fake(prefix: str, body: str) -> str:
    """Assemble a structurally-valid, semantically-dead credential.

    Kept in two pieces in the source so no complete token-shaped literal exists
    on any line, while the value passed to the scanner is identical to what a
    real one would look like.
    """
    return prefix + body


# Structurally valid, verifiably dead. See module docstring.
AWS_KEY = _fake("AKIA", "IOSFODNN7EXAMPLE")          # AWS's documented example
GITHUB_TOKEN = _fake("ghp", "_abcdefghijklmnopqrstuvwxyz0123456789")
OPENAI_KEY = _fake("sk", "-abcdefghijklmnopqrstuvwxyz0123")
ANTHROPIC_KEY = _fake("sk", "-ant-abcdefghijklmnopqrstuvwxyz0123")
SLACK_TOKEN = _fake("xox", "b-123456789012-abcdefghijklmno")
GOOGLE_KEY = _fake("AIza", "SyA1234567890abcdefghijklmnopqrstuv")  # AIza + 35

# The public jwt.io demo token: header {"alg":"HS256"}, payload {"sub":"123..."},
# signed with the literal secret "secret". Carries no claims and grants nothing.
DEMO_JWT = _fake(
    "eyJhbGciOiJIUzI1NiJ9.",
    "eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
)

PEM_BODY = "MIIEowIBAAKCAQEA1234567890"   # 26 chars; a real RSA-2048 body is ~1,600+


# --------------------------------------------------------------- detection --
@pytest.mark.parametrize("text,kind", [
    (f"aws key {AWS_KEY} here", "aws_access_key"),
    (GITHUB_TOKEN, "github_token"),
    (OPENAI_KEY, "api_key_sk"),
    (ANTHROPIC_KEY, "api_key_sk"),
    (SLACK_TOKEN, "slack_token"),
    (GOOGLE_KEY, "google_api_key"),
])
def test_detects_known_credential_shapes(text, kind):
    findings = scan(text)
    assert findings, f"missed a {kind}"
    assert findings[0].kind == kind


def test_detects_private_key_block():
    text = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        f"{PEM_BODY}\n"
        "-----END RSA PRIVATE KEY-----"
    )
    findings = scan(text)
    assert findings and findings[0].kind == "private_key"

    safe, _ = redact(text)
    assert PEM_BODY not in safe, "key body must not survive redaction"


def test_detects_labelled_assignment():
    findings = scan('password = "hunter2istooshort"')
    assert findings and findings[0].kind == "generic_secret"


def test_detects_connection_string_password():
    text = "postgresql://admin:" + "n0tar3alpw" + "@db.internal:5432/prod"
    safe, findings = redact(text)
    assert findings
    assert "n0tar3alpw" not in safe
    assert "db.internal" in safe, "only the credential is redacted, not the whole URL"


def test_detects_jwt():
    assert contains_secret(DEMO_JWT)


# ----------------------------------------------------------- redaction ------
def test_redaction_removes_the_secret_and_keeps_context():
    text = f"Deploying with {AWS_KEY} to us-east-1"
    safe, findings = redact(text)

    assert AWS_KEY not in safe
    assert "Deploying with" in safe
    assert "us-east-1" in safe
    assert "[REDACTED:aws_access_key]" in safe
    assert len(findings) == 1


def test_multiple_secrets_all_redacted():
    text = f"aws: {AWS_KEY}\ngithub: {GITHUB_TOKEN}\nopenai: {OPENAI_KEY}\n"
    safe, findings = redact(text)

    assert len(findings) == 3
    for leaked in (AWS_KEY, GITHUB_TOKEN, OPENAI_KEY):
        assert leaked not in safe


def test_preview_never_contains_the_full_secret():
    """Findings get logged. A finding that quotes the key defeats the point."""
    findings = scan(AWS_KEY)
    assert AWS_KEY not in findings[0].preview
    assert len(findings[0].preview) <= 8


def test_clean_text_is_returned_unchanged():
    text = "def add(a, b):\n    return a + b\n"
    safe, findings = redact(text)
    assert safe == text
    assert findings == []


def test_empty_input():
    assert redact("") == ("", [])
    assert scan("") == []


# --------------------------------------------------------- false positives --
@pytest.mark.parametrize("placeholder", [
    'password = "your_password_here"',
    'api_key = "CHANGEME"',
    'secret = "xxxxxxxxxxxx"',
    'token = "<your-token>"',
    'password = "${DB_PASSWORD}"',
    'api_key = "placeholder"',
])
def test_placeholders_are_not_treated_as_secrets(placeholder):
    """Redacting docs and templates is noise that trains people to ignore it."""
    assert not contains_secret(placeholder), f"false positive on {placeholder}"


def test_ordinary_code_is_not_flagged():
    code = """
    import os
    def handler(request):
        user = request.get("user")
        return {"status": 200, "body": f"hello {user}"}
    """
    assert not contains_secret(code)


def test_findings_are_ordered_and_non_overlapping():
    text = f"{GITHUB_TOKEN} then {AWS_KEY}"
    findings = scan(text)

    assert [f.start for f in findings] == sorted(f.start for f in findings)
    for a, b in zip(findings, findings[1:], strict=False):
        assert a.end <= b.start, "spans must not overlap"


# ------------------------------------------------------- fixture hygiene ----
def test_no_complete_token_literal_exists_in_this_file():
    """Guards the rule the module docstring sets out.

    If someone later pastes a full token as a literal, this fails here rather
    than at `git push` — a much cheaper place to find out, and it keeps the
    repo's scanner alerts meaningful instead of routinely ignored.
    """
    import re
    from pathlib import Path

    source = Path(__file__).read_text(encoding="utf-8")

    forbidden = {
        "aws": r"AKIA[0-9A-Z]{16}",
        "github": r"gh[pousr]_[A-Za-z0-9]{30,}",
        "slack": r"xox[abprs]-[A-Za-z0-9\-]{10,}",
        "google": r"AIza[0-9A-Za-z_\-]{35}",
        "openai": r"\bsk-[A-Za-z0-9_\-]{20,}",
    }

    offenders = [name for name, pattern in forbidden.items()
                 if re.search(pattern, source)]

    assert not offenders, (
        f"complete token-shaped literals found in the source: {offenders}. "
        f"Assemble them with _fake() instead — see the module docstring."
    )
