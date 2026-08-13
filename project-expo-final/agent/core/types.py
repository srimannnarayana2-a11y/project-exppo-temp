"""
Core shared types used across the orchestrator, pivot loop, and critique.

These are ORCHESTRATOR-level types (SubagentInput/SubagentResult), not
block-internal types (BlockInput/NodeResult from blocks/semantic/types.py).
The adapter in blocks/base.py bridges the two.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class SubagentType(str, Enum):
    RETRIEVER = "retriever"
    CODE_RETRIEVER = "code_retriever"
    SANDBOX = "sandbox"
    FILE_GENERATOR = "file_generator"


@dataclass
class SubagentInput:
    """Rigid tool interface for all subagents."""
    task: str
    subagent_type: SubagentType
    payload: dict = field(default_factory=dict)
    session_id: Optional[str] = None
    turn_id: Optional[str] = None
    parent_id: Optional[str] = None


@dataclass
class SubagentResult:
    """Rigid result interface from all subagents."""
    subagent_type: SubagentType
    success: bool
    learnings: list = field(default_factory=list)
    source_urls: list = field(default_factory=list)
    error_reason: str = ""
    session_id: Optional[str] = None
    turn_id: Optional[str] = None
    parent_id: Optional[str] = None


@dataclass
class Hypothesis:
    """One competing explanation for why an action failed."""
    label: str
    explanation: str
    prior: float = 0.5
    implies_circuit_break: str = ""


@dataclass
class PivotDecision:
    """Output of the pivot loop."""
    goal: str
    confirmed_hypothesis: Optional[Hypothesis] = None
    next_action: str = ""
    circuit_break: list[str] = field(default_factory=list)


@dataclass
class Learning:
    """One fact extracted from retrieval, with provenance and relevance."""
    text: str
    source_url: str = ""
    score: float = 0.0
