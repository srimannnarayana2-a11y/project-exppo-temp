"""
Sandbox block — gives the agent HANDS.

From chats:
  L288: "agent should do bash thing"
  L340: "acts like an agent like how Claude Code uses terminal inside
         itself to create files and present them"
  L344: "build fully without errors, 100% complete to deployment"
  L344: "it should know connections, concepts, compatibility,
         consequences of changing a line"

This block:
  1. Gets a task ("create student portal website")
  2. Plans file structure (planner.py)
  3. Generates code for each file (LLM)
  4. Writes files via agent_tools
  5. Validates completeness (validator.py)
  6. If issues → fix loop (max 3 rounds)
  7. Returns created files + structure
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

from ...llm.client import NIMClient, get_client
from ...tools.agent_tools import (
    ToolResult, bash_tool, file_write_tool, file_read_tool, list_dir_tool,
)
from .planner import plan_file_structure, FileTree, PlannedFile
from .validator import validate_completeness

logger = logging.getLogger(__name__)

MAX_FIX_ROUNDS = 3


@dataclass
class SandboxResult:
    """Result of a sandbox execution."""
    success: bool
    files_created: list[str] = field(default_factory=list)
    file_tree: Optional[FileTree] = None
    preview_url: str = ""
    errors: list[str] = field(default_factory=list)
    fix_rounds_used: int = 0


@dataclass
class SandboxInput:
    """Input to the sandbox block."""
    task: str
    sandbox_dir: str = ""
    sandbox_fn: Optional[object] = None  # injected E2B/Firecracker
    context: str = ""                     # additional context for planning


_CODE_GEN_PROMPT = (
    "You are a senior developer. Write the COMPLETE code for this file.\n\n"
    "RULES:\n"
    "1. COMPLETE: No TODO, no placeholders, no '...' — every function "
    "implemented, every style defined, every route handled.\n"
    "2. PRODUCTION QUALITY: Real error handling, proper structure.\n"
    "3. WORKING: The code must run. No imports of nonexistent modules.\n"
    "4. CONSISTENT: Follow the project structure and reference other "
    "files correctly (imports, links, etc.).\n\n"
    "Return ONLY the file content, no markdown fences, no explanation."
)


async def sandbox_block(
    inp: SandboxInput,
    *,
    client: Optional[NIMClient] = None,
) -> SandboxResult:
    """Main sandbox execution loop.

    Plan → Create → Validate → Fix → Return
    """
    client = client or get_client()

    if not inp.sandbox_dir and not inp.sandbox_fn:
        return SandboxResult(success=False, errors=["No sandbox_dir or sandbox_fn configured"])

    # ── Step 1: Plan file structure ──
    logger.info("Sandbox: planning file structure for '%s'", inp.task[:80])
    file_tree = await plan_file_structure(inp.task, client=client, context=inp.context)
    logger.info("Sandbox: planned %d files, entry=%s", len(file_tree.files), file_tree.entry_point)

    # ── Step 2: Generate + write each file ──
    files_created = []
    file_contents: dict[str, str] = {}

    for planned_file in file_tree.files:
        code = await _generate_file_code(
            planned_file, file_tree, file_contents, inp.task, client,
        )
        file_contents[planned_file.path] = code

        result = await file_write_tool(
            planned_file.path,
            code,
            sandbox_dir=inp.sandbox_dir,
            sandbox_fn=inp.sandbox_fn,
        )

        if result.success:
            files_created.append(planned_file.path)
        else:
            logger.warning("Failed to write %s: %s", planned_file.path, result.error)

    # ── Step 3: Validate + Fix loop ──
    fix_rounds = 0
    for round_num in range(MAX_FIX_ROUNDS):
        issues = await validate_completeness(
            inp.task, file_tree, file_contents, client=client,
        )

        if not issues:
            logger.info("Sandbox: validation passed (round %d)", round_num)
            break

        fix_rounds = round_num + 1
        logger.info("Sandbox: %d issues found, fixing (round %d)", len(issues), fix_rounds)

        # Fix each issue
        for issue in issues[:5]:  # Cap fixes per round
            fixed_code = await _fix_file(
                issue, file_contents, file_tree, inp.task, client,
            )
            if fixed_code and issue.get("file"):
                file_contents[issue["file"]] = fixed_code
                await file_write_tool(
                    issue["file"], fixed_code,
                    sandbox_dir=inp.sandbox_dir,
                    sandbox_fn=inp.sandbox_fn,
                )

    return SandboxResult(
        success=len(files_created) > 0,
        files_created=files_created,
        file_tree=file_tree,
        fix_rounds_used=fix_rounds,
    )


async def _generate_file_code(
    planned: PlannedFile,
    tree: FileTree,
    existing_contents: dict[str, str],
    task: str,
    client: NIMClient,
) -> str:
    """Generate code for one file, aware of the full project context."""
    # Build context from files already created (dependency-aware)
    dep_context = ""
    for dep_path in planned.depends_on:
        if dep_path in existing_contents:
            content = existing_contents[dep_path]
            # Truncate large files to save tokens
            if len(content) > 2000:
                content = content[:2000] + "\n... (truncated)"
            dep_context += f"\n--- {dep_path} ---\n{content}\n"

    # Project overview for context
    other_files = "\n".join(f"- {f.path}: {f.description}" for f in tree.files)

    messages = [
        {"role": "system", "content": _CODE_GEN_PROMPT},
        {"role": "user", "content": (
            f"PROJECT: {task}\n"
            f"TECH STACK: {', '.join(tree.tech_stack)}\n"
            f"ALL FILES:\n{other_files}\n\n"
            f"NOW WRITE: {planned.path}\n"
            f"DESCRIPTION: {planned.description}\n"
            f"LANGUAGE: {planned.language}\n"
            f"{f'DEPENDENCIES ALREADY WRITTEN:{dep_context}' if dep_context else ''}"
        )},
    ]

    return await client.chat(messages, temperature=0.1, max_tokens=4096)


async def _fix_file(
    issue: dict,
    file_contents: dict[str, str],
    tree: FileTree,
    task: str,
    client: NIMClient,
) -> Optional[str]:
    """Fix a specific issue in a file."""
    file_path = issue.get("file", "")
    if not file_path or file_path not in file_contents:
        return None

    current_code = file_contents[file_path]

    messages = [
        {"role": "system", "content": (
            "Fix the issue in this file. Return ONLY the complete fixed file "
            "content, no markdown fences, no explanation. The fix must not "
            "break anything else — be aware of the full project structure."
        )},
        {"role": "user", "content": (
            f"PROJECT: {task}\n"
            f"FILE: {file_path}\n"
            f"ISSUE: {issue.get('description', 'Unknown issue')}\n"
            f"SEVERITY: {issue.get('severity', 'medium')}\n\n"
            f"CURRENT CODE:\n{current_code}"
        )},
    ]

    return await client.chat(messages, temperature=0.1, max_tokens=4096)
