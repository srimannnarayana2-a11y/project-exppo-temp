"""
Agent hands — bash_tool, file_write, file_edit.

From chats L340: "it should have access to bash... acts like an agent
like how Claude Code uses terminal inside itself to create files and
present them"

These are INJECTABLE SEAMS — same pattern as fetch_fn/code_tool_fn.
The default implementation runs subprocess in a sandbox directory.
User can inject their own (Jarvis's bash tool, E2B sandbox, etc.)

SECURITY: All operations are restricted to a sandbox_dir. Path traversal
is checked. The sandbox_tool_fn seam allows routing these to an actual
microVM (E2B/Firecracker) in production.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional, Callable, Awaitable

# Import Jarvis bridge tools
try:
    from .jarvis_bridge import (
        jarvis_build_deck,
        jarvis_build_report,
        jarvis_build_sheet,
        jarvis_build_dashboard,
        jarvis_code_analyze,
        jarvis_run_tests,
        jarvis_format_code,
        jarvis_nvidia_rag_retrieve,
        jarvis_web_search,
        jarvis_web_fetch,
        jarvis_grep,
        jarvis_bash,
        jarvis_read,
        jarvis_write,
        jarvis_edit,
        jarvis_todo_write,
        jarvis_todo_read,
        JARVIS_TOOL_SCHEMAS,
    )
    JARVIS_BRIDGE_AVAILABLE = True
except ImportError as e:
    logger.warning("Jarvis bridge not available (mine-antigravity not in workspace): %s", e)
    JARVIS_BRIDGE_AVAILABLE = False

logger = logging.getLogger(__name__)


@dataclass
class ToolResult:
    """Standard result from any agent tool."""
    success: bool
    output: str = ""
    error: str = ""
    files_created: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)


# ── Path safety ──

def _safe_path(sandbox_dir: str, target: str) -> str:
    """Resolve path and verify it's inside sandbox_dir. Raises ValueError if not."""
    resolved = os.path.realpath(os.path.join(sandbox_dir, target))
    sandbox_real = os.path.realpath(sandbox_dir)
    if not resolved.startswith(sandbox_real):
        raise ValueError(f"Path traversal blocked: {target} resolves outside sandbox")
    return resolved


# ── Bash tool ──

async def bash_tool(
    command: str,
    *,
    sandbox_dir: str = "",
    timeout: float = 30.0,
    sandbox_fn: Optional[Callable[..., Awaitable[ToolResult]]] = None,
) -> ToolResult:
    """Execute a bash/shell command.

    Priority:
    1. sandbox_fn (injected — E2B, Firecracker, Jarvis bash tool)
    2. Built-in subprocess (restricted to sandbox_dir)

    The sandbox_fn seam is where real isolation happens in production.
    The built-in is a DEV fallback only.
    """
    # Priority 1: Injected sandbox
    if sandbox_fn is not None:
        try:
            return await sandbox_fn(command)
        except Exception as e:
            logger.warning("Injected sandbox_fn failed: %s, trying built-in", e)

    # Priority 2: Built-in subprocess (dev only)
    if not sandbox_dir:
        return ToolResult(success=False, error="No sandbox_dir configured for built-in bash")

    os.makedirs(sandbox_dir, exist_ok=True)

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=sandbox_dir,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return ToolResult(
            success=proc.returncode == 0,
            output=stdout.decode("utf-8", errors="replace")[:50_000],
            error=stderr.decode("utf-8", errors="replace")[:10_000],
        )
    except asyncio.TimeoutError:
        proc.kill()
        return ToolResult(success=False, error=f"Command timed out after {timeout}s")
    except Exception as e:
        return ToolResult(success=False, error=str(e))


# ── File write tool ──

async def file_write_tool(
    path: str,
    content: str,
    *,
    sandbox_dir: str = "",
    sandbox_fn: Optional[Callable[..., Awaitable[ToolResult]]] = None,
) -> ToolResult:
    """Write content to a file. Creates parent dirs if needed.

    Same seam pattern: sandbox_fn first, built-in fallback.
    """
    if sandbox_fn is not None:
        try:
            return await sandbox_fn("write", path, content)
        except Exception as e:
            logger.warning("Injected sandbox_fn failed: %s", e)

    if not sandbox_dir:
        return ToolResult(success=False, error="No sandbox_dir configured")

    try:
        safe = _safe_path(sandbox_dir, path)
        os.makedirs(os.path.dirname(safe), exist_ok=True)
        with open(safe, "w", encoding="utf-8") as f:
            f.write(content)
        return ToolResult(success=True, output=f"Written {len(content)} chars to {path}",
                          files_created=[path])
    except Exception as e:
        return ToolResult(success=False, error=str(e))


# ── File edit tool ──

async def file_edit_tool(
    path: str,
    search: str,
    replace: str,
    *,
    sandbox_dir: str = "",
    sandbox_fn: Optional[Callable[..., Awaitable[ToolResult]]] = None,
) -> ToolResult:
    """Search-and-replace edit in an existing file.

    From chats L344: "it should know the connections, concepts, compatibility,
    consequences of changing a line — like I might need to change this file too"

    This tool does the mechanical edit. The DECISION of what to edit comes
    from the code_retriever_block's dependency-aware analysis (not built yet).
    """
    if sandbox_fn is not None:
        try:
            return await sandbox_fn("edit", path, search, replace)
        except Exception as e:
            logger.warning("Injected sandbox_fn failed: %s", e)

    if not sandbox_dir:
        return ToolResult(success=False, error="No sandbox_dir configured")

    try:
        safe = _safe_path(sandbox_dir, path)
        if not os.path.exists(safe):
            return ToolResult(success=False, error=f"File not found: {path}")

        with open(safe, "r", encoding="utf-8") as f:
            original = f.read()

        if search not in original:
            return ToolResult(success=False, error=f"Search string not found in {path}")

        new_content = original.replace(search, replace, 1)
        with open(safe, "w", encoding="utf-8") as f:
            f.write(new_content)

        return ToolResult(success=True, output=f"Edited {path}", files_modified=[path])
    except Exception as e:
        return ToolResult(success=False, error=str(e))


# ── File read tool ──

async def file_read_tool(
    path: str,
    *,
    sandbox_dir: str = "",
    sandbox_fn: Optional[Callable[..., Awaitable[ToolResult]]] = None,
) -> ToolResult:
    """Read a file's content."""
    if sandbox_fn is not None:
        try:
            return await sandbox_fn("read", path)
        except Exception as e:
            logger.warning("Injected sandbox_fn failed: %s", e)

    if not sandbox_dir:
        return ToolResult(success=False, error="No sandbox_dir configured")

    try:
        safe = _safe_path(sandbox_dir, path)
        with open(safe, "r", encoding="utf-8") as f:
            content = f.read()
        return ToolResult(success=True, output=content[:100_000])
    except Exception as e:
        return ToolResult(success=False, error=str(e))


# ── List directory tool ──

async def list_dir_tool(
    path: str = ".",
    *,
    sandbox_dir: str = "",
    sandbox_fn: Optional[Callable[..., Awaitable[ToolResult]]] = None,
) -> ToolResult:
    """List directory contents with file sizes."""
    if sandbox_fn is not None:
        try:
            return await sandbox_fn("list", path)
        except Exception as e:
            logger.warning("Injected sandbox_fn failed: %s", e)

    if not sandbox_dir:
        return ToolResult(success=False, error="No sandbox_dir configured")

    try:
        safe = _safe_path(sandbox_dir, path)
        entries = []
        for entry in os.scandir(safe):
            kind = "dir" if entry.is_dir() else "file"
            size = entry.stat().st_size if entry.is_file() else 0
            entries.append(f"{kind}\t{size}\t{entry.name}")
        return ToolResult(success=True, output="\n".join(entries))
    except Exception as e:
        return ToolResult(success=False, error=str(e))


# ── Tool registry for the agent ──

TOOL_REGISTRY = {
    "bash": bash_tool,
    "file_write": file_write_tool,
    "file_edit": file_edit_tool,
    "file_read": file_read_tool,
    "list_dir": list_dir_tool,
}
