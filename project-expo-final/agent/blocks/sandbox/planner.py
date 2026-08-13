"""
Sandbox planner — plans file structures before creating them.

From chats L340: "it should create file structure and do all that"
From chats A27 (geohash): overview first, then drill into each file.

The planner produces a FileTree that the sandbox block creates
file-by-file in dependency order.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Optional

from ...llm.client import NIMClient, get_client

logger = logging.getLogger(__name__)


@dataclass
class PlannedFile:
    """One file in the planned structure."""
    path: str                        # e.g. "src/index.html"
    description: str                 # what this file does
    depends_on: list[str] = field(default_factory=list)  # paths it imports/references
    language: str = ""               # "html", "css", "js", "python", etc.
    priority: int = 0                # lower = create first


@dataclass
class FileTree:
    """Complete planned file structure."""
    files: list[PlannedFile] = field(default_factory=list)
    overview: str = ""               # high-level description
    tech_stack: list[str] = field(default_factory=list)
    entry_point: str = ""            # main file to run/open


_PLAN_PROMPT = (
    "You are a software architect. Given a task, plan the COMPLETE file "
    "structure needed to fulfill it. Think about what a senior developer "
    "would create — no missing files, no placeholders.\n\n"
    "Return a JSON object with:\n"
    "- overview: 1-2 sentence description of the project\n"
    "- tech_stack: array of technologies used\n"
    "- entry_point: the main file to run or open\n"
    "- files: array of objects, each with:\n"
    "  - path: relative file path\n"
    "  - description: what this file does (1 sentence)\n"
    "  - depends_on: array of other file paths this imports/references\n"
    "  - language: file language\n\n"
    "RULES:\n"
    "1. COMPLETE: Include every file needed. Config, styles, scripts, assets.\n"
    "2. PRODUCTION READY: Real code structure, not tutorials.\n"
    "3. DEPENDENCY ORDER: List dependencies first.\n"
    "4. NO PLACEHOLDERS: Every file must be implementable.\n\n"
    "Return ONLY valid JSON, no markdown fences."
)


async def plan_file_structure(
    task: str,
    *,
    client: Optional[NIMClient] = None,
    context: str = "",
) -> FileTree:
    """Plan the file structure for a task.

    GeoHash approach: first get the overview/structure,
    THEN the sandbox block implements each file one by one.
    """
    client = client or get_client()

    messages = [
        {"role": "system", "content": _PLAN_PROMPT},
        {"role": "user", "content": f"Task: {task}\n\n{f'Context: {context}' if context else ''}"},
    ]

    raw = await client.chat(messages, temperature=0.2, max_tokens=2048)

    # Parse JSON response
    try:
        # Strip markdown fences if present
        text = raw.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            if text.endswith("```"):
                text = text[:-3]
        data = json.loads(text)
    except (json.JSONDecodeError, IndexError):
        logger.warning("Plan response not valid JSON, extracting manually")
        return FileTree(
            overview=f"Plan for: {task}",
            files=[PlannedFile(path="index.html", description="Main file")],
        )

    files = []
    for i, f in enumerate(data.get("files", [])):
        files.append(PlannedFile(
            path=f.get("path", f"file_{i}"),
            description=f.get("description", ""),
            depends_on=f.get("depends_on", []),
            language=f.get("language", ""),
            priority=i,
        ))

    # Sort by dependency: files with no deps first
    files = _topological_sort(files)

    return FileTree(
        files=files,
        overview=data.get("overview", ""),
        tech_stack=data.get("tech_stack", []),
        entry_point=data.get("entry_point", files[0].path if files else ""),
    )


def _topological_sort(files: list[PlannedFile]) -> list[PlannedFile]:
    """Sort files so dependencies come before dependents."""
    path_to_file = {f.path: f for f in files}
    visited: set[str] = set()
    result: list[PlannedFile] = []

    def visit(path: str):
        if path in visited:
            return
        visited.add(path)
        f = path_to_file.get(path)
        if f:
            for dep in f.depends_on:
                if dep in path_to_file:
                    visit(dep)
            result.append(f)

    for f in files:
        visit(f.path)

    # Add any remaining (shouldn't happen, but safety)
    for f in files:
        if f not in result:
            result.append(f)

    return result
