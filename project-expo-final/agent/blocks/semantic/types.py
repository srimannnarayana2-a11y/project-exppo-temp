"""
Shared dataclasses for the semantic retriever block.

These are the block's internal types — NOT the orchestrator-level contracts
(SubagentInput/SubagentResult). The adapter in blocks/base.py bridges the two.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable, Optional


class Mode(str, Enum):
    PUBLIC = "public"
    KB = "kb"


@dataclass
class BlockInput:
    """Input to one recursive node of the semantic retriever block."""

    query: Optional[str] = None          # semantic query
    url: Optional[str] = None            # direct URL — skips search entirely
    mode: Mode = Mode.PUBLIC
    depth: int = 0
    max_depth: int = 3
    node_timeout_s: float = 8.0
    global_deadline: Optional[float] = None

    # Dynamic depth extension (rare escape valve, not routine)
    extensions_used: int = 0
    max_extensions_per_branch: int = 1
    extension_increment: int = 1
    min_time_margin_s: float = 3.0

    # Injectable seams — user's tools, or None for built-in fallback
    fetch_fn: Optional[Callable] = None
    code_tool_fn: Optional[Callable] = None


@dataclass
class Chunk:
    """One chunk of retrieved content with metadata."""

    text: str
    source_url: str
    score: float = 0.0
    embedding: Optional[list[float]] = None
    title: str = ""
    position: int = 0  # position within parent document


@dataclass
class Decision:
    """Output of the per-node decision LLM."""

    sufficient: bool                     # True → terminate this branch
    reason: str
    next_queries: list[str] = field(default_factory=list)
    next_mode: Optional[Mode] = None     # allows pivot public↔kb
    needs_code_retriever: bool = False

    # Dynamic complexity — only meaningful at depth ceiling
    request_extension: bool = False
    extension_justification: str = ""


@dataclass
class NodeResult:
    """Result of one recursive node."""

    query: str
    learnings: list[str]
    source_urls: list[str]
    children: list["NodeResult"] = field(default_factory=list)
    terminated_reason: str = ""
    # "depth_budget" | "low_info_gain" | "timeout_or_error" |
    # "no_results" | "leaf_answered" | "expanded" | "global_deadline"


@dataclass
class Learning:
    """One fact extracted from retrieval, with provenance."""

    text: str
    source_url: str = ""
    score: float = 0.0


# Type aliases for injectable seams
FetchFn = Callable[[str], Awaitable[str]]
CodeToolFn = Callable[[str], Awaitable[NodeResult]]
