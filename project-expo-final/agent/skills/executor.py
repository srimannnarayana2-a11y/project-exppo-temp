"""
Skill executor — runs a matched skill with available tools.

From research: "Skills are high-level workflows that USE tools."

Flow:
  1. Load SKILL.md instructions (progressive disclosure)
  2. Pass instructions + query + tools to LLM
  3. LLM generates a step-by-step plan
  4. Execute each step using available tools
  5. Validate output (critique if needed)
  6. Return formatted result

The LLM acts as the "brain" that interprets skill instructions
and decides which tools to call, in what order, with what params.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from ..llm.client import NIMClient, get_client
from ..core.types import Learning
from .registry import SkillMatch, get_skill_registry

logger = logging.getLogger(__name__)


@dataclass
class SkillResult:
    """Result of executing a skill."""
    success: bool
    output: str = ""                    # main text output
    files_created: list[str] = field(default_factory=list)
    rendered_file: bytes = b""          # rendered output (PDF/PPTX/etc)
    rendered_format: str = ""           # format of rendered_file
    learnings: list[Learning] = field(default_factory=list)
    steps_completed: int = 0
    total_steps: int = 0
    error: str = ""


_EXECUTOR_PROMPT = (
    "You are executing a skill. You have specific instructions below "
    "on HOW to complete this task. Follow them carefully.\n\n"
    "Your available tools:\n"
    "- brave_search(query): search the web for information\n"
    "- file_write(path, content): create a file\n"
    "- file_read(path): read a file\n"
    "- bash(command): run a shell command\n"
    "- render_output(markdown, format): convert markdown to PDF/DOCX/PPTX\n\n"
    "Generate your response as a structured plan, then the full output.\n"
    "For file-creation skills: output the COMPLETE file content.\n"
    "For research skills: output the COMPLETE research report.\n"
    "For analysis skills: output the COMPLETE analysis with insights.\n\n"
    "CRITICAL: Be COMPLETE. No placeholders, no TODOs, no '...'.\n"
    "The user expects a finished product, not a draft."
)


async def execute_skill(
    match: SkillMatch,
    query: str,
    *,
    client: Optional[NIMClient] = None,
    context: str = "",
    output_format: str = "markdown",
) -> SkillResult:
    """Execute a matched skill.

    Steps:
      1. Load full instructions from SKILL.md
      2. Build prompt with instructions + query + context
      3. LLM generates the complete output
      4. If output format requested, render it
      5. Return result
    """
    client = client or get_client()
    registry = get_skill_registry()

    # ── Step 1: Load instructions (progressive disclosure) ──
    instructions = registry.load_instructions(match)
    if not instructions:
        return SkillResult(
            success=False,
            error=f"Could not load instructions for skill: {match.skill.name}",
        )

    logger.info("Executing skill '%s' (score=%.2f)", match.skill.name, match.score)

    # ── Step 2: Research phase (if skill needs it) ──
    research_context = ""
    if "brave_search" in match.skill.tools_required:
        research_context = await _research_for_skill(query, client)

    # ── Step 3: Generate output via LLM ──
    messages = [
        {"role": "system", "content": _EXECUTOR_PROMPT},
        {"role": "user", "content": (
            f"## SKILL: {match.skill.name}\n\n"
            f"## INSTRUCTIONS\n{instructions}\n\n"
            f"## USER REQUEST\n{query}\n\n"
            f"{f'## RESEARCH CONTEXT{chr(10)}{research_context}' if research_context else ''}\n"
            f"{f'## ADDITIONAL CONTEXT{chr(10)}{context}' if context else ''}\n\n"
            f"## OUTPUT FORMAT: {output_format}\n\n"
            "Now execute this skill completely."
        )},
    ]

    try:
        output = await client.chat(messages, temperature=0.2, max_tokens=4096)
    except Exception as e:
        return SkillResult(success=False, error=f"LLM error: {e}")

    # ── Step 4: Render if needed ──
    rendered_file = b""
    rendered_format = ""

    if output_format != "markdown":
        try:
            from ..tools.output_renderer import render_output
            result = render_output(output, output_format, match.skill.name)
            rendered_file = result.content
            rendered_format = result.format_name
        except Exception as e:
            logger.warning("Render failed: %s", e)

    return SkillResult(
        success=True,
        output=output,
        rendered_file=rendered_file,
        rendered_format=rendered_format,
        learnings=[Learning(text=f"Skill '{match.skill.name}' executed for: {query[:100]}")],
        steps_completed=1,
        total_steps=1,
    )


async def _research_for_skill(query: str, client: NIMClient) -> str:
    """Quick research pass for skills that need web data."""
    try:
        from ..tools.brave_search import brave_search
        brave_results = await brave_search(query)

        snippets = []

        # Use LLM context results first (best quality)
        for ctx in brave_results.context_results[:3]:
            snippets.append(f"- [{ctx.title}]({ctx.url}): {ctx.text[:200]}")

        # Then web results
        for r in brave_results.web_results[:5]:
            snippet = r.snippet or ""
            if r.extra_snippets:
                snippet += " " + " ".join(r.extra_snippets[:2])
            if snippet:
                snippets.append(f"- [{r.title}]({r.url}): {snippet[:200]}")

        return "\n".join(snippets[:8])
    except Exception as e:
        logger.warning("Research for skill failed: %s", e)
        return ""

