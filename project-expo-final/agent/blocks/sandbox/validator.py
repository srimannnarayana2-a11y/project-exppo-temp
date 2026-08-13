"""
Sandbox validator — ensures completeness before returning to user.

From chats L344: "build fully without errors, 100% complete to deployment"
From chats L353-355: critique personas verify, but unanimous ≠ correct.

Uses the critique system (brutal_critic, realist) to check if the
generated project is actually complete, not just "looks done".
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from ...llm.client import NIMClient, get_client
from .planner import FileTree

logger = logging.getLogger(__name__)

_VALIDATE_PROMPT = (
    "You are a brutal code reviewer. You've been given a task and the files "
    "created for it. Your job is to find REAL issues — not style nitpicks, "
    "but things that would make this project NOT WORK or NOT BE COMPLETE.\n\n"
    "Check for:\n"
    "1. MISSING FILES: Are there files the project needs that weren't created?\n"
    "2. BROKEN REFERENCES: Do files import/link things that don't exist?\n"
    "3. INCOMPLETE FEATURES: Does the task ask for something that isn't built?\n"
    "4. RUNTIME ERRORS: Will this crash when run? Missing variables, bad syntax?\n"
    "5. MISSING STYLES: Does the UI exist but look broken (no CSS, no layout)?\n\n"
    "Return a JSON array of issues. Each issue:\n"
    "- file: which file has the problem (or 'project' for missing files)\n"
    "- description: what's wrong (specific, not vague)\n"
    "- severity: 'critical' | 'major' | 'minor'\n\n"
    "If everything looks complete and working, return an empty array: []\n"
    "Return ONLY valid JSON, no markdown."
)


async def validate_completeness(
    task: str,
    file_tree: FileTree,
    file_contents: dict[str, str],
    *,
    client: Optional[NIMClient] = None,
) -> list[dict]:
    """Validate that the created project is complete and working.

    Returns list of issues (empty = all good).
    """
    client = client or get_client()

    # Build file summary for the reviewer
    files_summary = []
    for path, content in file_contents.items():
        # Show first 100 lines or 3000 chars to save tokens
        truncated = content[:3000]
        if len(content) > 3000:
            truncated += f"\n... ({len(content) - 3000} more chars)"
        files_summary.append(f"=== {path} ===\n{truncated}")

    all_files = "\n\n".join(files_summary)
    planned_files = "\n".join(f"- {f.path}: {f.description}" for f in file_tree.files)

    messages = [
        {"role": "system", "content": _VALIDATE_PROMPT},
        {"role": "user", "content": (
            f"TASK: {task}\n\n"
            f"PLANNED FILES:\n{planned_files}\n\n"
            f"ACTUAL FILES CREATED:\n{all_files}"
        )},
    ]

    raw = await client.chat(messages, temperature=0.1, max_tokens=1024)

    try:
        text = raw.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            if text.endswith("```"):
                text = text[:-3]
        issues = json.loads(text)
        if not isinstance(issues, list):
            return []
        # Only return critical and major issues
        return [i for i in issues if i.get("severity") in ("critical", "major")]
    except (json.JSONDecodeError, IndexError):
        logger.warning("Validation response not valid JSON")
        return []
