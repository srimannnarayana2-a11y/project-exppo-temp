"""
Skill registry — discovers, indexes, and matches skills.

Industry pattern: PROGRESSIVE DISCLOSURE
  - Only skill name + description stay in memory
  - Full SKILL.md instructions loaded ONLY when triggered
  - Prevents context window bloat with 50+ skills

From research:
  "Skills are NOT tools — tools are low-level (bash, file_write),
   skills are high-level workflows that USE tools."

Discovery:
  1. Scan builtin/ directory for SKILL.md files
  2. Scan user skills/ directory (if configured)
  3. Parse YAML frontmatter for name, description, triggers
  4. Match incoming queries against trigger patterns
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class SkillMeta:
    """Lightweight skill metadata — kept in memory for matching.
    Full instructions loaded on-demand from SKILL.md."""
    name: str
    description: str
    triggers: list[str] = field(default_factory=list)
    tools_required: list[str] = field(default_factory=list)
    skill_dir: str = ""           # path to the skill directory
    skill_md_path: str = ""       # path to SKILL.md

    # Compiled trigger patterns (cached)
    _compiled: list[re.Pattern] = field(default_factory=list, repr=False)

    def compile_triggers(self):
        """Compile trigger keywords into regex patterns."""
        self._compiled = []
        for trigger in self.triggers:
            # Convert trigger keyword to flexible regex
            # "presentation" matches "make a presentation", "create presentation", etc.
            pattern = re.compile(
                rf'\b{re.escape(trigger)}\b',
                re.IGNORECASE,
            )
            self._compiled.append(pattern)

    def match_score(self, query: str) -> float:
        """How well does this skill match the query? 0.0 = no match."""
        if not self._compiled:
            self.compile_triggers()

        matches = sum(1 for p in self._compiled if p.search(query))
        if matches == 0:
            return 0.0

        # Score: proportion of triggers matched, weighted by description similarity
        trigger_score = matches / len(self._compiled) if self._compiled else 0
        # Bonus if query contains skill name
        name_bonus = 0.2 if self.name.lower() in query.lower() else 0.0

        return min(trigger_score + name_bonus, 1.0)


@dataclass
class SkillMatch:
    """Result of matching a query to skills."""
    skill: SkillMeta
    score: float
    instructions: str = ""  # loaded on demand


def _parse_skill_md(path: str) -> Optional[SkillMeta]:
    """Parse a SKILL.md file: YAML frontmatter + markdown body."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        logger.warning("Cannot read %s: %s", path, e)
        return None

    # Parse YAML frontmatter (between --- markers)
    frontmatter = {}
    body = content

    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            yaml_text = parts[1].strip()
            body = parts[2].strip()

            # Simple YAML parser (avoid pyyaml dependency)
            for line in yaml_text.split("\n"):
                line = line.strip()
                if ":" in line:
                    key, _, val = line.partition(":")
                    key = key.strip()
                    val = val.strip()

                    # Handle lists: [item1, item2]
                    if val.startswith("[") and val.endswith("]"):
                        items = [
                            v.strip().strip('"').strip("'")
                            for v in val[1:-1].split(",")
                        ]
                        frontmatter[key] = items
                    else:
                        frontmatter[key] = val.strip('"').strip("'")

    if not frontmatter.get("name"):
        return None

    skill_dir = os.path.dirname(path)

    meta = SkillMeta(
        name=frontmatter.get("name", ""),
        description=frontmatter.get("description", ""),
        triggers=frontmatter.get("triggers", []),
        tools_required=frontmatter.get("tools_required", []),
        skill_dir=skill_dir,
        skill_md_path=path,
    )
    meta.compile_triggers()
    return meta


class SkillRegistry:
    """Discovers and indexes skills. Progressive disclosure:
    only metadata in memory, full instructions loaded on trigger."""

    def __init__(self):
        self._skills: list[SkillMeta] = []

    @property
    def count(self) -> int:
        return len(self._skills)

    @property
    def skill_names(self) -> list[str]:
        return [s.name for s in self._skills]

    def discover(self, *directories: str):
        """Scan directories for SKILL.md files and index them."""
        for directory in directories:
            if not os.path.isdir(directory):
                logger.debug("Skill directory not found: %s", directory)
                continue

            for entry in os.listdir(directory):
                skill_dir = os.path.join(directory, entry)
                skill_md = os.path.join(skill_dir, "SKILL.md")

                if os.path.isfile(skill_md):
                    meta = _parse_skill_md(skill_md)
                    if meta:
                        self._skills.append(meta)
                        logger.info("Discovered skill: %s (%d triggers)",
                                    meta.name, len(meta.triggers))

        logger.info("Skill registry: %d skills discovered", len(self._skills))

    def match(self, query: str, threshold: float = 0.1) -> Optional[SkillMatch]:
        """Find the best matching skill for a query.

        Returns None if no skill scores above threshold.
        Progressive disclosure: instructions NOT loaded yet.
        """
        best_skill = None
        best_score = 0.0

        for skill in self._skills:
            score = skill.match_score(query)
            if score > best_score:
                best_score = score
                best_skill = skill

        if best_skill and best_score >= threshold:
            return SkillMatch(skill=best_skill, score=best_score)

        return None

    def load_instructions(self, match: SkillMatch) -> str:
        """Load full SKILL.md instructions for an activated skill.

        This is the "progressive disclosure" step — only called when
        a skill is actually triggered, not during discovery.
        """
        try:
            with open(match.skill.skill_md_path, "r", encoding="utf-8") as f:
                content = f.read()

            # Strip YAML frontmatter, return only the instruction body
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    match.instructions = parts[2].strip()
                    return match.instructions

            match.instructions = content
            return content
        except Exception as e:
            logger.warning("Cannot load skill instructions: %s", e)
            return ""

    def register(self, meta: SkillMeta):
        """Manually register a skill (for testing or dynamic skills)."""
        meta.compile_triggers()
        self._skills.append(meta)


# ── Module singleton ──

_registry: Optional[SkillRegistry] = None


def get_skill_registry() -> SkillRegistry:
    """Get or create the global skill registry."""
    global _registry
    if _registry is None:
        _registry = SkillRegistry()
        # Auto-discover builtin skills
        builtin_dir = os.path.join(os.path.dirname(__file__), "builtin")
        _registry.discover(builtin_dir)
    return _registry
