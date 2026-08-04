"""Citation-consistency verifier (BUILD_PLAN step 35).

[HAND] — the second verifier. Checks whether generated text references
only the known source documents. Does NOT call a model — the rule holds:
a verifier reaches its verdict from observable output.

The verifier is deliberately limited to exact-match citation checking.
A production version would use fuzzy matching and entity extraction, but
this stub demonstrates the plugin interface and is sufficient to prove
that the system supports multiple verifier domains.

Task type: "research" — the first code_change verifier catches code bugs;
this catches citation fabrications.
"""

from __future__ import annotations

import contextlib
import re
import time
from dataclasses import dataclass

from bucker.contracts.models import Task, WorkerResult
from bucker.sandbox.runtime import DockerSandbox
from bucker.verifiers.base import VerificationResult, register

#: Match citations like [Smith 2020] or (Jones et al., 2019).
CITATION_RE = re.compile(
    r"\[([A-Z][a-z]+(?:\s+(?:et\s+al\.?|&\s+[A-Z][a-z]+))?\s*,?\s*\d{4}[a-z]?)\]"
    r"|"
    r"\(([A-Z][a-z]+(?:\s+(?:et\s+al\.?|&\s+[A-Z][a-z]+))?\s*,?\s*\d{4}[a-z]?)\)",
)

#: Match a DOI.
DOI_RE = re.compile(r"10\.\d{4,}/[^\s]+")


def extract_citations(text: str) -> list[str]:
    """Extract bracketed and parenthetical citations from text."""
    matches: list[str] = []
    for m in CITATION_RE.finditer(text):
        cite = m.group(1) or m.group(2)
        matches.append(cite.strip())
    return matches


def extract_dois(text: str) -> list[str]:
    """Extract DOIs from text."""
    return DOI_RE.findall(text)


def check_against_sources(
    citations: list[str],
    sources: list[str],
) -> dict:
    """Check each citation against the known source list.

    Returns a dict with lists of valid and unknown citations.
    """
    source_lower = {s.lower() for s in sources}
    unknown = [c for c in citations if c.lower() not in source_lower]
    valid = [c for c in citations if c.lower() in source_lower]
    return {
        "valid_citations": valid,
        "unknown_citations": unknown,
        "total_citations": len(citations),
        "unknown_count": len(unknown),
    }


@dataclass(slots=True)
class CitationVerifier:
    """Checks that generated text only references known sources."""

    name: str = "citation_checker"
    task_types: tuple[str, ...] = ("research",)
    timeout_s: int = 30

    async def verify(
        self,
        task: Task,
        result: WorkerResult,
        sandbox: DockerSandbox,
    ) -> VerificationResult:
        started = time.perf_counter()

        if result.status == "blocked":
            return VerificationResult(
                passed=False,
                verifier=self.name,
                diagnostics=f"worker blocked: {result.blocked_reason}",
                details={"blocked": True},
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

        # Read the output from the workspace.
        output_text = ""
        sources_text = ""

        if result.files_touched:
            for path in result.files_touched:
                with contextlib.suppress(Exception):
                    output_text += sandbox.read_file(path) + "\n"

        # Try to read a sources file if it exists.
        with contextlib.suppress(Exception):
            sources_text = sandbox.read_file("sources.txt")

        sources = [s.strip() for s in sources_text.split("\n") if s.strip()]
        citations = extract_citations(output_text)

        if not citations:
            return VerificationResult(
                passed=True,
                verifier=self.name,
                diagnostics="no citations found — nothing to check",
                details={"citations": 0},
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

        check = check_against_sources(citations, sources)
        passed = check["unknown_count"] == 0

        if passed:
            diag = f"All {check['total_citations']} citations match known sources"
        else:
            diag = (
                f"{check['unknown_count']}/{check['total_citations']} citations "
                f"not found in sources: {', '.join(check['unknown_citations'][:5])}"
            )

        return VerificationResult(
            passed=passed,
            verifier=self.name,
            diagnostics=diag,
            details=check,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )


def register_citation_verifier() -> None:
    """Register this verifier so it shows up in available_verifiers()."""
    register(CitationVerifier())
