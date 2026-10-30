"""Skills loaded from the workspace `skills/` directory.

A skill is a markdown file with YAML frontmatter: `name` and `description` say when it applies,
the body is the procedure. Files are read once at construction and never reloaded, and `list()`
omits bodies so the model can choose a skill without paying for all of them.
"""

import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)


@dataclass(frozen=True, slots=True)
class Skill:
    name: str
    description: str
    body: str

    def summary(self) -> "Skill":
        """The skill without its body, for a list the model picks from."""
        return Skill(name=self.name, description=self.description, body="")


class SkillStore:
    """All skills of one workspace, keyed by name in filename order."""

    def __init__(self, skills_dir: Path) -> None:
        self.skills_dir = Path(skills_dir)
        self._skills: dict[str, Skill] = {}
        if not self.skills_dir.is_dir():
            return
        for path in sorted(self.skills_dir.glob("*.md")):
            skill = _load(path)
            if skill is None:
                continue
            if skill.name in self._skills:
                logger.warning("duplicate skill name %r in %s; keeping the first", skill.name, path)
                continue
            self._skills[skill.name] = skill

    def list(self) -> list[Skill]:
        return [skill.summary() for skill in self._skills.values()]

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def __contains__(self, name: object) -> bool:
        return name in self._skills

    def __len__(self) -> int:
        return len(self._skills)


def _load(path: Path) -> Skill | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        logger.warning("cannot read skill %s: %s", path, e)
        return None
    match = _FRONTMATTER.match(text)
    if match is None:
        logger.warning("skill %s has no YAML frontmatter", path)
        return None
    fields = _frontmatter(match.group(1))
    name = fields.get("name") or path.stem
    return Skill(name=name, description=fields.get("description", ""), body=text[match.end() :].strip())


def _frontmatter(block: str) -> dict[str, str]:
    """Flat `key: value` pairs only; indented lines continue the previous value."""
    fields: dict[str, str] = {}
    key: str | None = None
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line[:1] in (" ", "\t") and key is not None:
            fields[key] = f"{fields[key]} {stripped}".strip()
            continue
        name, sep, value = line.partition(":")
        if not sep:
            continue
        key = name.strip()
        fields[key] = _unquote(value.strip())
    return fields


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value
