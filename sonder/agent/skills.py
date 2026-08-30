"""Skill loading with progressive disclosure.

A skill is a directory with a SKILL.md and optional supporting files. Only
the SKILL.md body is loaded by default; supporting files are pulled in when
the router asks for them by name. With a dozen domain skills, loading
everything would cost more context than the conversation being analysed.

Skills contain knowledge and method. They never contain a person, a
preference, or a history -- that is State, and mixing the two is how a
"skill" quietly becomes an unversioned second memory.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import structlog

from ..config import settings

log = structlog.get_logger(__name__)

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass(slots=True)
class Skill:
    name: str
    description: str
    triggers: list[str]
    body: str
    path: Path
    files: dict[str, Path] = field(default_factory=dict)
    priority: int = 50

    def load_file(self, name: str) -> str | None:
        p = self.files.get(name)
        return p.read_text(encoding="utf-8") if p else None

    @property
    def approx_tokens(self) -> int:
        return len(self.body) // 3


def _parse(path: Path) -> Skill | None:
    md = path / "SKILL.md"
    if not md.exists():
        return None
    raw = md.read_text(encoding="utf-8")
    meta: dict[str, str] = {}
    m = _FRONTMATTER.match(raw)
    body = raw
    if m:
        body = raw[m.end() :]
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
    files = {f.name: f for f in path.glob("*.md") if f.name != "SKILL.md"}
    files.update({f"cases/{f.name}": f for f in (path / "cases").glob("*.md")})
    return Skill(
        name=meta.get("name", path.name),
        description=meta.get("description", ""),
        triggers=[t.strip() for t in meta.get("triggers", "").split(",") if t.strip()],
        body=body.strip(),
        path=path,
        files=files,
        priority=int(meta.get("priority", 50)),
    )


@lru_cache(maxsize=1)
def registry() -> dict[str, Skill]:
    root = settings().skills_dir
    if not root.exists():
        log.warning("skills_dir_missing", path=str(root))
        return {}
    out: dict[str, Skill] = {}
    for d in sorted(root.iterdir()):
        if d.is_dir() and (s := _parse(d)):
            out[s.name] = s
    log.info("skills_loaded", count=len(out), names=sorted(out))
    return out


# Which skills each intent may draw on, most important first. Explicit rather
# than similarity-matched: a wrong skill silently changes the agent's whole
# frame, and that failure is not worth the flexibility.
INTENT_SKILLS: dict[str, tuple[str, ...]] = {
    "REPLY": ("reasoning", "dating-core", "flirting", "communication"),
    "ANALYZE_CONVERSATION": ("reasoning", "dating-core", "psychology", "communication"),
    "RELATIONSHIP_REVIEW": ("reasoning", "relationship-science", "dating-core"),
    "STRATEGY": ("reasoning", "dating-core", "flirting", "relationship-science"),
    "CONFLICT": ("communication", "relationship-science", "psychology"),
    "SELF_REFLECTION": ("psychology", "philosophy", "reasoning"),
    "DATE_PLANNING": ("dating-core", "communication"),
    "COMPATIBILITY": ("relationship-science", "reasoning"),
    "INTIMACY": ("reasoning", "intimacy", "communication", "relationship-science"),
}

DEFAULT_BUDGET_TOKENS = 6000


def select(intent: str, budget_tokens: int = DEFAULT_BUDGET_TOKENS) -> list[Skill]:
    """Pick skills for an intent, newest-first within a context budget."""
    reg = registry()
    wanted = INTENT_SKILLS.get(intent, ("reasoning", "dating-core"))
    picked: list[Skill] = []
    used = 0
    for name in wanted:
        s = reg.get(name)
        if s is None:
            continue
        if used + s.approx_tokens > budget_tokens and picked:
            log.debug("skill_budget_reached", skipped=name, used=used)
            break
        picked.append(s)
        used += s.approx_tokens
    return picked


def render(skills: list[Skill]) -> str:
    if not skills:
        return ""
    parts = [
        "The following domain knowledge applies. It describes method, never "
        "a specific person -- everything about the people involved comes from "
        "the state block, not from here.\n"
    ]
    for s in skills:
        parts.append(f'\n<skill name="{s.name}">\n{s.body}\n</skill>')
    return "\n".join(parts)
