"""Procedural memory: skills the worker can apply.

A skill is a markdown file (``skills/<name>/SKILL.md``, Hermes-style)
describing a procedure: when to use it, the steps, the pitfalls. When a
task's objective matches a skill, the skill's procedure is injected into
the worker prompt as part of working memory — the model follows the
proven procedure instead of improvising.

Frontmatter is YAML-ish and minimal:

    ---
    name: fix-failing-tests
    description: Repair a failing test suite by reading errors first
    ---
    # when to use
    ...conditions...
    # procedure
    1. run the tests and read the first failure
    2. ...

The registry is a dumb scorer: keyword overlap between the objective and
the skill's description + name. Skills are data — add one by dropping a
file in; the worker only changes behavior when a skill actually matches.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import Path

_FRONTMATTER_RE = re.compile(
    r"^---\nname: (.+)\ndescription: (.+)\n---\n", re.M
)


@dataclass(frozen=True, slots=True)
class Skill:
    name: str
    description: str
    body: str

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "body": self.body,
        }


class SkillStore:
    """File-backed skill registry (procedural memory)."""

    #: Overridable default root (tests point this at a tmp dir).
    default_root: Path | None = None

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root else Path(
            self.default_root
            or Path(__file__).resolve().parent.parent.parent / "skills"
        )
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ writes ----
    def add(self, name: str, description: str, body: str) -> Skill:
        """Create a skill. Names must be safe slugs; overwrites on clash."""
        name = name.strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,63}", name):
            raise ValueError(
                "skill name must be a slug: lowercase letters, digits, "
                "dash or underscore, 3-64 chars"
            )
        description = description.strip()
        body = body.strip()
        if not description or not body:
            raise ValueError("skill needs a description and a procedure body")
        skill = Skill(name=name, description=description, body=body)
        path = self.root / name / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
            encoding="utf-8",
        )
        return skill

    # ------------------------------------------------------------- reads ----
    def list(self) -> list[Skill]:
        skills = []
        for path in sorted(self.root.glob("*/SKILL.md")):
            parsed = self._parse(path)
            if parsed:
                skills.append(parsed)
        return skills

    def get(self, name: str) -> Skill | None:
        path = self.root / name / "SKILL.md"
        if not path.exists():
            return None
        return self._parse(path)

    def remove(self, name: str) -> bool:
        path = self.root / name / "SKILL.md"
        if not path.exists():
            return False
        path.unlink()
        return True

    def count(self) -> int:
        return len(self.list())

    # ------------------------------------------------------------ context ----
    def for_objective(self, objective: str, limit: int = 3) -> list[Skill]:
        """Skills whose name/description overlap the objective, capped."""
        words = {w for w in re.findall(r"[a-z0-9_]{4,}", objective.lower())}
        scored = []
        for skill in self.list():
            hay = f"{skill.name} {skill.description}".lower()
            overlap = len(words & set(re.findall(r"[a-z0-9_]{4,}", hay)))
            if overlap:
                scored.append((overlap, skill))
        scored.sort(key=lambda x: -x[0])
        return [s for _, s in scored[:limit]]

    # ------------------------------------------------------------ internal ----
    def _parse(self, path: Path) -> Skill | None:
        text = path.read_text(encoding="utf-8")
        m = _FRONTMATTER_RE.search(text)
        if not m:
            return None
        body = text[m.end():].strip()
        return Skill(name=m.group(1).strip(),
                     description=m.group(2).strip(),
                     body=body)


def default_skill() -> Skill:
    """A starter skill shipped with the platform (proves the mechanism)."""
    return Skill(
        name="verify-before-done",
        description=(
            "never claim completion without running the project's own "
            "checks first; read failures before writing fixes"
        ),
        body=(
            "# when to use\n"
            "Any task whose objective mentions tests, checks, or passing.\n\n"
            "# procedure\n"
            "1. Run the project's checks first; read the FIRST failure.\n"
            "2. Fix the root cause of that failure, not its symptom.\n"
            "3. Re-run the checks; repeat until they pass.\n"
            "4. Never claim done on an unverified result — the verifier is "
            "the judge, not the claim.\n"
        ),
    )


def seed_default_skills(store: SkillStore) -> list[str]:
    """Install the starter skills that are missing. Returns created names."""
    created = []
    if store.get("verify-before-done") is None:
        store.add("verify-before-done",
                  default_skill().description, default_skill().body)
        created.append("verify-before-done")
    return created


def unused_uuid() -> str:  # keep import surface honest
    return str(uuid.uuid4())
