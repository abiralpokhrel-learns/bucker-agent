"""Semantic memory: durable facts about the user and the project.

The Hermes-inspired piece: facts persist across sessions as markdown
files in ``memory/`` (one file per fact, human-readable, git-ignored —
local persistence, the user owns the data). Facts are injected into the
planner prompt as context, so a task knows what the platform has learned:
"this project's tests run with pytest", "the model is ollama/qwen2.5-coder".

A fact is one line of durable truth plus provenance:

    ## <fact id>
    - text: the fact itself
    - source: who/what established it (user | consolidate:<task_id>)
    - created: ISO timestamp

The store is deliberately dumb: keyword matching, no embeddings. Small
files, grep-able, inspectable — exactly what a memory system should be
before it needs to be clever.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

_FACT_RE = re.compile(
    r"^## ([0-9a-f-]{36})\n- text: (.*)\n- source: (.*)\n- created: (.*)$",
    re.M,
)


class MemoryStore:
    """File-backed semantic memory. Each fact is one markdown file."""

    #: Overridable default root (tests point this at a tmp dir).
    default_root: Path | None = None

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root else Path(
            self.default_root
            or Path(__file__).resolve().parent.parent.parent / "memory"
        )
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ writes ----
    def add(self, text: str, source: str = "user") -> str:
        """Store a fact. Returns its id. Empty text is rejected."""
        text = text.strip().replace("\n", " ")
        if not text:
            raise ValueError("fact text must not be empty")
        fact_id = str(uuid.uuid4())
        # Microsecond resolution: prune()'s "keep newest" ordering must be
        # stable even for facts added in the same clock second.
        created = datetime.now(UTC).isoformat(timespec="microseconds")
        path = self.root / f"{fact_id}.md"
        path.write_text(
            f"## {fact_id}\n- text: {text}\n- source: {source}\n- created: {created}\n",
            encoding="utf-8",
        )
        return fact_id

    # ------------------------------------------------------------- reads ----
    def list(self) -> list[dict]:
        facts = []
        for path in sorted(self.root.glob("*.md")):
            parsed = self._parse(path)
            if parsed:
                facts.append(parsed)
        return facts

    def search(self, query: str) -> list[dict]:
        """Keyword match against fact text (case-insensitive)."""
        q = query.lower()
        return [f for f in self.list() if q in f["text"].lower()]

    def get(self, fact_id: str) -> dict | None:
        path = self.root / f"{fact_id}.md"
        if not path.exists():
            return None
        return self._parse(path)

    def remove(self, fact_id: str) -> bool:
        path = self.root / f"{fact_id}.md"
        if not path.exists():
            return False
        path.unlink()
        return True

    def count(self) -> int:
        return len(self.list())

    # -------------------------------------------------------------- prune ----
    def prune(self, limit: int = 200) -> list[str]:
        """Self-curation (Hermes-style): bound the store.

        Two passes, both conservative:
          1. dedupe — facts whose normalized text is IDENTICAL keep only
             the newest (never merges different facts);
          2. cap — beyond ``limit``, oldest facts are removed.
        Returns the ids removed. A memory that grows forever dilutes
        context injection, so this is the harness's garbage collector.
        """
        removed: list[str] = []
        seen: dict[str, str] = {}
        for fact in sorted(self.list(), key=lambda f: f["created"], reverse=True):
            sig = re.sub(r"[^a-z0-9]+", " ", fact["text"].lower()).strip()
            if sig in seen:
                removed.append(fact["id"])
                self.remove(fact["id"])
            else:
                seen[sig] = fact["id"]

        overflow = sorted(self.list(), key=lambda f: f["created"])[:-limit] \
            if limit > 0 else []
        for fact in overflow:
            removed.append(fact["id"])
            self.remove(fact["id"])
        return removed

    # ------------------------------------------------------------ context ----
    def context_for(self, objective: str, limit: int = 5) -> list[dict]:
        """Facts relevant to an objective: keyword overlap, capped."""
        words = {w for w in re.findall(r"[a-z0-9_]{4,}", objective.lower())}
        scored = []
        for fact in self.list():
            fw = set(re.findall(r"[a-z0-9_]{4,}", fact["text"].lower()))
            overlap = len(words & fw)
            if overlap:
                scored.append((overlap, fact))
        scored.sort(key=lambda x: -x[0])
        return [f for _, f in scored[:limit]]

    # ------------------------------------------------------------ internal ----
    def _parse(self, path: Path) -> dict | None:
        text = path.read_text(encoding="utf-8")
        m = _FACT_RE.search(text)
        if not m:
            return None
        return {
            "id": m.group(1),
            "text": m.group(2),
            "source": m.group(3),
            "created": m.group(4),
        }
