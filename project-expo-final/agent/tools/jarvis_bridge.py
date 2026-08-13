"""
Jarvis Bridge — Wraps TypeScript Jarvis tools for use in the Python Agent.

This module provides async wrappers around all 26 Jarvis tools, enabling:
- Builder tools (BuildDeck, BuildReport, BuildSheet, BuildDashboard)
- Code analysis tools (CodeAnalyze, RunTests, FormatCode)
- Advanced RAG (NvidiaRagRetrieve with adaptive retrieval)
- File operations (Read, Write, Edit, LS, Bash)
- Search tools (WebSearch, WebFetch, Grep, Glob)
- Utilities (TodoWrite, TodoRead, Skill execution)

Tools are called via subprocess (bun run), with result caching to avoid
repeated expensive operations.

Architecture:
  1. Tool call → json serialize args
  2. Subprocess: bun run jarvis-cli.ts {tool_name} {args_json}
  3. Parse result → ToolResult dataclass
  4. Cache hit on repeated calls (configurable TTL)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from typing import Optional, Any
from pathlib import Path
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# ─── Tool Result Dataclass ───────────────────────────────────────────────────

@dataclass
class JarvisToolResult:
    """Standard result from any Jarvis tool call."""
    success: bool
    output: str = ""
    error: str = ""
    files_created: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "output": self.output,
            "error": self.error,
            "files_created": self.files_created,
            "files_modified": self.files_modified,
            "metadata": self.metadata,
        }


# ─── Tool Cache ─────────────────────────────────────────────────────────────

class JarvisToolCache:
    """LRU-style cache for Jarvis tool results with TTL."""

    def __init__(self, default_ttl_seconds: int = 300):
        self._cache: dict[str, tuple[JarvisToolResult, datetime]] = {}
        self.default_ttl = timedelta(seconds=default_ttl_seconds)
        self.hits = 0
        self.misses = 0

    def _key(self, tool_name: str, args: dict) -> str:
        """Create cache key from tool name and args."""
        # Convert args to a stable JSON string
        args_str = json.dumps(args, sort_keys=True, default=str)
        return f"{tool_name}:{args_str}"

    def get(self, tool_name: str, args: dict) -> Optional[JarvisToolResult]:
        """Retrieve cached result if fresh, else None."""
        key = self._key(tool_name, args)
        if key not in self._cache:
            self.misses += 1
            return None

        result, timestamp = self._cache[key]
        if datetime.now() - timestamp > self.default_ttl:
            self.misses += 1
            del self._cache[key]
            return None

        self.hits += 1
        logger.debug(f"Cache hit for {tool_name}")
        return result

    def set(self, tool_name: str, args: dict, result: JarvisToolResult):
        """Store result in cache."""
        key = self._key(tool_name, args)
        self._cache[key] = (result, datetime.now())

    def clear(self):
        """Clear all cached results."""
        self._cache.clear()

    def stats(self) -> dict:
        """Return cache statistics."""
        total = self.hits + self.misses
        hit_rate = (self.hits / total * 100) if total > 0 else 0
        return {
            "hits": self.hits,
            "misses": self.misses,
            "total": total,
            "hit_rate_percent": hit_rate,
            "cached_keys": len(self._cache),
        }


_jarvis_cache = JarvisToolCache(default_ttl_seconds=600)


# ─── Jarvis CLI Bridge ─────────────────────────────────────────────────────

async def call_jarvis_tool(
    tool_name: str,
    args: dict,
    cwd: Optional[str] = None,
    timeout: float = 30.0,
    use_cache: bool = True,
) -> JarvisToolResult:
    """
    Call a Jarvis tool via subprocess (bun run jarvis-cli.ts).

    Args:
        tool_name: Name of the Jarvis tool (e.g., "BuildDeck", "Read", "WebSearch")
        args: Tool arguments as dict
        cwd: Working directory for the tool
        timeout: Subprocess timeout in seconds
        use_cache: Whether to check/store in cache

    Returns:
        JarvisToolResult with success flag and output/error
    """

    # Check cache first
    if use_cache:
        cached = _jarvis_cache.get(tool_name, args)
        if cached:
            logger.debug(f"Returning cached result for {tool_name}")
            return cached

    # Find Jarvis CLI entrypoint
    jarvis_dir = Path(__file__).parent.parent.parent / "mine-antigravity" / "mine-antigravity"
    jarvis_cli = jarvis_dir / "jarvis-cli.ts"

    if not jarvis_cli.exists():
        return JarvisToolResult(
            success=False,
            error=f"Jarvis CLI not found at {jarvis_cli}. Ensure mine-antigravity is in workspace."
        )

    # Prepare subprocess call
    if cwd is None:
        cwd = str(Path.cwd())

    try:
        # Serialize args to JSON
        args_json = json.dumps(args)

        # Call: bun run jarvis-cli.ts {tool_name} {args_json}
        proc = await asyncio.create_subprocess_exec(
            "bun",
            "run",
            str(jarvis_cli),
            tool_name,
            args_json,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            result = JarvisToolResult(
                success=False,
                error=f"Jarvis tool '{tool_name}' timed out after {timeout}s"
            )
            return result

        # Parse result
        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")

        if proc.returncode != 0:
            result = JarvisToolResult(
                success=False,
                error=stderr_text or stdout_text
            )
        else:
            # Try to parse JSON result
            try:
                result_json = json.loads(stdout_text)
                result = JarvisToolResult(**result_json)
            except json.JSONDecodeError:
                # Fallback: treat stdout as output
                result = JarvisToolResult(
                    success=True,
                    output=stdout_text
                )

        # Cache the result
        if use_cache:
            _jarvis_cache.set(tool_name, args, result)

        return result

    except Exception as e:
        return JarvisToolResult(
            success=False,
            error=f"Error calling Jarvis tool '{tool_name}': {str(e)}"
        )


# ─── Individual Tool Wrappers ────────────────────────────────────────────────

async def jarvis_read(
    file_path: str,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
    cwd: Optional[str] = None,
) -> JarvisToolResult:
    """Read a file or file range from disk."""
    args = {"file_path": file_path}
    if start_line is not None:
        args["start_line"] = start_line
    if end_line is not None:
        args["end_line"] = end_line
    return await call_jarvis_tool("Read", args, cwd=cwd)


async def jarvis_write(
    file_path: str,
    content: str,
    cwd: Optional[str] = None,
) -> JarvisToolResult:
    """Write content to a file."""
    return await call_jarvis_tool("Write", {"file_path": file_path, "content": content}, cwd=cwd)


async def jarvis_edit(
    file_path: str,
    old_string: str,
    new_string: str,
    cwd: Optional[str] = None,
) -> JarvisToolResult:
    """Edit a file (search and replace)."""
    return await call_jarvis_tool(
        "Edit",
        {"file_path": file_path, "old_string": old_string, "new_string": new_string},
        cwd=cwd
    )


async def jarvis_web_search(
    query: str,
    top_k: int = 5,
    cwd: Optional[str] = None,
) -> JarvisToolResult:
    """Search the web."""
    return await call_jarvis_tool("WebSearch", {"query": query, "top_k": top_k}, cwd=cwd)


async def jarvis_web_fetch(
    url: str,
    max_chars: int = 50000,
    cwd: Optional[str] = None,
) -> JarvisToolResult:
    """Fetch a URL and return its content as text."""
    return await call_jarvis_tool("WebFetch", {"url": url, "max_chars": max_chars}, cwd=cwd)


async def jarvis_grep(
    pattern: str,
    path: str,
    include: Optional[str] = None,
    case_sensitive: bool = False,
    cwd: Optional[str] = None,
) -> JarvisToolResult:
    """Search for a pattern in files."""
    args = {"pattern": pattern, "path": path, "case_sensitive": case_sensitive}
    if include is not None:
        args["include"] = include
    return await call_jarvis_tool("Grep", args, cwd=cwd)


async def jarvis_code_analyze(
    file_path: str,
    cwd: Optional[str] = None,
) -> JarvisToolResult:
    """Analyze a code file (LOC, imports, functions, TODOs)."""
    return await call_jarvis_tool("CodeAnalyze", {"file_path": file_path}, cwd=cwd)


async def jarvis_run_tests(
    path: Optional[str] = None,
    command: Optional[str] = None,
    cwd: Optional[str] = None,
) -> JarvisToolResult:
    """Run tests in a directory or with a specific command."""
    args = {}
    if path is not None:
        args["path"] = path
    if command is not None:
        args["command"] = command
    return await call_jarvis_tool("RunTests", args, cwd=cwd)


async def jarvis_format_code(
    file_path: str,
    cwd: Optional[str] = None,
) -> JarvisToolResult:
    """Format code in a file (prettier, rustfmt, black, etc.)."""
    return await call_jarvis_tool("FormatCode", {"file_path": file_path}, cwd=cwd)


async def jarvis_build_deck(
    spec: dict | str,
    output_path: Optional[str] = None,
    cwd: Optional[str] = None,
) -> JarvisToolResult:
    """Build a PowerPoint presentation from a spec."""
    args = {"spec": spec}
    if output_path is not None:
        args["output_path"] = output_path
    return await call_jarvis_tool("BuildDeck", args, cwd=cwd)


async def jarvis_build_report(
    spec: dict | str,
    formats: Optional[list[str]] = None,
    output_path: Optional[str] = None,
    cwd: Optional[str] = None,
) -> JarvisToolResult:
    """Build a report (DOCX, PDF, etc.) from a spec."""
    args = {"spec": spec}
    if formats is not None:
        args["formats"] = formats
    if output_path is not None:
        args["output_path"] = output_path
    return await call_jarvis_tool("BuildReport", args, cwd=cwd)


async def jarvis_build_sheet(
    spec: dict | str,
    output_path: Optional[str] = None,
    cwd: Optional[str] = None,
) -> JarvisToolResult:
    """Build an Excel spreadsheet from a spec."""
    args = {"spec": spec}
    if output_path is not None:
        args["output_path"] = output_path
    return await call_jarvis_tool("BuildSheet", args, cwd=cwd)


async def jarvis_build_dashboard(
    spec: dict | str,
    output_path: Optional[str] = None,
    cwd: Optional[str] = None,
) -> JarvisToolResult:
    """Build an HTML dashboard from a spec."""
    args = {"spec": spec}
    if output_path is not None:
        args["output_path"] = output_path
    return await call_jarvis_tool("BuildDashboard", args, cwd=cwd)


async def jarvis_nvidia_rag_retrieve(
    query: str,
    top_k: int = 5,
    collection: Optional[str] = None,
    cwd: Optional[str] = None,
) -> JarvisToolResult:
    """Retrieve from NVIDIA RAG with adaptive retrieval."""
    args = {"query": query, "top_k": top_k}
    if collection is not None:
        args["collection"] = collection
    return await call_jarvis_tool("NvidiaRagRetrieve", args, cwd=cwd)


async def jarvis_todo_write(
    task: str,
    context: Optional[str] = None,
    cwd: Optional[str] = None,
) -> JarvisToolResult:
    """Write a task to the todo list."""
    args = {"task": task}
    if context is not None:
        args["context"] = context
    return await call_jarvis_tool("TodoWrite", args, cwd=cwd)


async def jarvis_todo_read(
    filter_str: Optional[str] = None,
    cwd: Optional[str] = None,
) -> JarvisToolResult:
    """Read from the todo list."""
    args = {}
    if filter_str is not None:
        args["filter"] = filter_str
    return await call_jarvis_tool("TodoRead", args, cwd=cwd)


async def jarvis_skill(
    name: str,
    input_data: dict,
    cwd: Optional[str] = None,
) -> JarvisToolResult:
    """Execute a Jarvis skill."""
    return await call_jarvis_tool("Skill", {"name": name, "input": input_data}, cwd=cwd)


async def jarvis_bash(
    command: str,
    timeout: float = 30.0,
    cwd: Optional[str] = None,
) -> JarvisToolResult:
    """Execute a bash command."""
    return await call_jarvis_tool("Bash", {"command": command}, cwd=cwd, timeout=timeout)


# ─── Cache Management ────────────────────────────────────────────────────────

def get_jarvis_cache_stats() -> dict:
    """Get Jarvis tool cache statistics."""
    return _jarvis_cache.stats()


def clear_jarvis_cache():
    """Clear all cached Jarvis tool results."""
    _jarvis_cache.clear()
    logger.info("Jarvis tool cache cleared")


# ─── Tool Definitions for LLM ───────────────────────────────────────────────

JARVIS_TOOL_SCHEMAS = {
    "BuildDeck": {
        "type": "function",
        "function": {
            "name": "BuildDeck",
            "description": "Create a PowerPoint presentation from a specification",
            "parameters": {
                "type": "object",
                "properties": {
                    "spec": {
                        "type": ["object", "string"],
                        "description": "Presentation spec (outline, content, formatting)"
                    },
                    "output_path": {
                        "type": "string",
                        "description": "Output file path (optional, defaults to ./output.pptx)"
                    },
                },
                "required": ["spec"]
            }
        }
    },
    "BuildReport": {
        "type": "function",
        "function": {
            "name": "BuildReport",
            "description": "Create a document report (DOCX, PDF, etc.) from a specification",
            "parameters": {
                "type": "object",
                "properties": {
                    "spec": {
                        "type": ["object", "string"],
                        "description": "Report spec (title, sections, content)"
                    },
                    "formats": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Output formats (pdf, docx, html)"
                    },
                    "output_path": {
                        "type": "string",
                        "description": "Output file path"
                    },
                },
                "required": ["spec"]
            }
        }
    },
    "BuildSheet": {
        "type": "function",
        "function": {
            "name": "BuildSheet",
            "description": "Create an Excel spreadsheet from a specification",
            "parameters": {
                "type": "object",
                "properties": {
                    "spec": {
                        "type": ["object", "string"],
                        "description": "Sheet spec (columns, rows, data, formulas)"
                    },
                    "output_path": {
                        "type": "string",
                        "description": "Output file path (optional)"
                    },
                },
                "required": ["spec"]
            }
        }
    },
    "BuildDashboard": {
        "type": "function",
        "function": {
            "name": "BuildDashboard",
            "description": "Create an HTML dashboard visualization from a specification",
            "parameters": {
                "type": "object",
                "properties": {
                    "spec": {
                        "type": ["object", "string"],
                        "description": "Dashboard spec (widgets, charts, layout)"
                    },
                    "output_path": {
                        "type": "string",
                        "description": "Output file path (optional)"
                    },
                },
                "required": ["spec"]
            }
        }
    },
    "CodeAnalyze": {
        "type": "function",
        "function": {
            "name": "CodeAnalyze",
            "description": "Analyze a code file to extract LOC, imports, functions, and TODOs",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to code file to analyze"
                    },
                },
                "required": ["file_path"]
            }
        }
    },
    "RunTests": {
        "type": "function",
        "function": {
            "name": "RunTests",
            "description": "Run tests in a directory (auto-detects test runner)",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to run tests from (optional)"
                    },
                    "command": {
                        "type": "string",
                        "description": "Explicit test command to run (optional)"
                    },
                },
                "required": []
            }
        }
    },
    "FormatCode": {
        "type": "function",
        "function": {
            "name": "FormatCode",
            "description": "Format code in a file using appropriate formatter",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to code file to format"
                    },
                },
                "required": ["file_path"]
            }
        }
    },
    "NvidiaRagRetrieve": {
        "type": "function",
        "function": {
            "name": "NvidiaRagRetrieve",
            "description": "Retrieve from NVIDIA Knowledge Base with adaptive RAG",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Query to retrieve documents for"
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Number of results to retrieve (default 5)"
                    },
                    "collection": {
                        "type": "string",
                        "description": "Specific knowledge base collection (optional)"
                    },
                },
                "required": ["query"]
            }
        }
    },
}


__all__ = [
    "JarvisToolResult",
    "JarvisToolCache",
    "call_jarvis_tool",
    # Individual tool wrappers
    "jarvis_read",
    "jarvis_write",
    "jarvis_edit",
    "jarvis_web_search",
    "jarvis_web_fetch",
    "jarvis_grep",
    "jarvis_code_analyze",
    "jarvis_run_tests",
    "jarvis_format_code",
    "jarvis_build_deck",
    "jarvis_build_report",
    "jarvis_build_sheet",
    "jarvis_build_dashboard",
    "jarvis_nvidia_rag_retrieve",
    "jarvis_todo_write",
    "jarvis_todo_read",
    "jarvis_skill",
    "jarvis_bash",
    # Cache management
    "get_jarvis_cache_stats",
    "clear_jarvis_cache",
    # Schemas for LLM
    "JARVIS_TOOL_SCHEMAS",
]
