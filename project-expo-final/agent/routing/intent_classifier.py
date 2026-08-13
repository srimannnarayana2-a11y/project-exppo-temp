"""
Intent Classifier — Smart Intent-Based Tool Routing

From Jarvis's `router.ts`:
  Classifies user requests into intent classes, then:
  1. Returns the optimal tool subset for that intent (reduces token bloat)
  2. Injects concrete usage examples into system prompt
  3. Provides confidence score and routing rationale

Intent classes (7):
  - build_document: presentations, reports, docs
  - code_task: reviews, fixes, explanations
  - data_task: spreadsheets, dashboards, charts
  - research: web/semantic search
  - file_op: read, write, edit, organize
  - system_cmd: git, npm, docker, tests
  - query: general Q&A

Returns:
  - intent: Classified intent
  - confidence: 0–1 confidence score
  - rationale: Why this intent
  - primary_tools: Ordered tool priority list
  - system_addendum: Injected before API call
  - should_plan_first: Boolean hint
  - should_verify: Boolean hint
  - parallelizable: Can tool calls run in parallel?

Usage:
    from agent.routing.intent_classifier import classify_intent
    from agent.tools.tool_registry import get_tools_for_intent
    
    result = classify_intent("build a quarterly report")
    print(result.intent)  # "build_document"
    print(result.primary_tools)  # ["build_report", "read_file", ...]
    
    tools = get_tools_for_intent(result.intent)
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


# ─── Intent Types ─────────────────────────────────────────────────────────────

INTENT_CLASSES = {
    "build_document",
    "code_task",
    "data_task",
    "research",
    "file_op",
    "system_cmd",
    "query",
}


@dataclass
class IntentClassification:
    """Result of intent classification."""
    intent: str                     # One of INTENT_CLASSES
    confidence: float               # 0–1
    rationale: str                  # Why this intent
    primary_tools: list[str]        # Ordered tool names for this intent
    system_addendum: str            # Injected into system prompt
    should_plan_first: bool = False # Recommend planning step
    should_verify: bool = False     # Recommend verification step
    parallelizable: bool = False    # Can tools run in parallel


# ─── Intent Patterns ──────────────────────────────────────────────────────────

class IntentPattern:
    """Pattern matcher for an intent class."""
    
    def __init__(self, intent: str, weight: int, patterns: list[str]):
        """
        Initialize pattern.
        
        Args:
            intent: Intent class name
            weight: Relative weight (higher = more specific)
            patterns: List of regex patterns (case-insensitive)
        """
        self.intent = intent
        self.weight = weight
        self.compiled_patterns = [
            re.compile(pattern, re.IGNORECASE)
            for pattern in patterns
        ]
    
    def match_score(self, query: str) -> float:
        """Score how well this pattern matches the query."""
        if not query:
            return 0.0
        
        # Count pattern matches
        matches = sum(1 for pattern in self.compiled_patterns if pattern.search(query))
        
        # Base score: weight × (matches / total patterns)
        base_score = self.weight * (matches / len(self.compiled_patterns)) if self.compiled_patterns else 0
        
        # Normalize to 0–1
        return min(1.0, base_score / 100)


# ─── Intent Pattern Registry ──────────────────────────────────────────────────

_INTENT_PATTERNS = [
    # ─── Build Document (10 — most specific) ───────────────────────────────────
    IntentPattern(
        "build_document",
        weight=10,
        patterns=[
            r"\b(pptx?|powerpoint|slide|deck|presentation)\b",
            r"\b(docx?|word\s+doc|report|document)\b",
            r"\b(pdf|export)\b",
            r"\b(make|create|build|generate|render|produce)\b.*\b(deck|slide|presentation|report|doc)\b",
            r"\b(pitch\s+deck|investor\s+deck|slide\s+deck)\b",
        ]
    ),
    
    # ─── Data Task (9) ─────────────────────────────────────────────────────────
    IntentPattern(
        "data_task",
        weight=9,
        patterns=[
            r"\b(xlsx?|excel|spreadsheet|csv)\b",
            r"\b(sheet|table|workbook|pivot)\b",
            r"\b(dashboard|chart|graph|plot|visualization)\b",
            r"\b(analyze|analyse)\b.*\b(data|csv|number|metric)\b",
            r"\b(make|create|build)\b.*\b(dashboard|chart|graph|sheet|spreadsheet)\b",
        ]
    ),
    
    # ─── System Command (8) ────────────────────────────────────────────────────
    IntentPattern(
        "system_cmd",
        weight=8,
        patterns=[
            r"\b(run|execute|start|stop|restart|kill)\b.*\b(test|server|script|command|process)\b",
            r"\b(install|uninstall|npm|bun|pip|yarn)\b",
            r"\b(build|compile|transpile|bundle)\b.*\b(project|code|app)\b",
            r"\b(git\s+(commit|push|pull|clone|status|log|diff|add))\b",
            r"\b(docker|kubectl|terraform)\b",
        ]
    ),
    
    # ─── Code Task (7) ─────────────────────────────────────────────────────────
    IntentPattern(
        "code_task",
        weight=7,
        patterns=[
            r"\b(review|audit|analyze)\b.*\b(code|function|class|implementation)\b",
            r"\b(explain|help|understand)\b.*\b(code|algorithm|pattern|syntax)\b",
            r"\b(fix|debug|bug|error|crash)\b",
            r"\b(refactor|optimize|improve|cleanup)\b.*\b(code|function|logic)\b",
            r"\b(write|implement|code)\b.*\b(function|class|algorithm|feature)\b",
        ]
    ),
    
    # ─── File Operation (6) ────────────────────────────────────────────────────
    IntentPattern(
        "file_op",
        weight=6,
        patterns=[
            r"\b(read|open|view|get)\b.*\b(file|document|log|config)\b",
            r"\b(write|save|create|overwrite)\b.*\b(file|document)\b",
            r"\b(edit|update|modify|change)\b.*\b(file|document|text)\b",
            r"\b(list|ls|dir|find)\b.*\b(file|directory|folder)\b",
            r"\b(organize|rename|move|copy|delete)\b.*\b(file|folder|directory)\b",
        ]
    ),
    
    # ─── Research (5) ──────────────────────────────────────────────────────────
    IntentPattern(
        "research",
        weight=5,
        patterns=[
            r"\b(search|look\s+up|find|query|research|investigate)\b",
            r"\b(what|how|why|where)\b.*\b(is|are|was|were)\b",
            r"\b(latest|current|recent|news|article|paper)\b",
            r"\b(stack\s+overflow|github|documentation|tutorial)\b",
        ]
    ),
    
    # ─── Default Query (1 — catch-all) ────────────────────────────────────────
    IntentPattern(
        "query",
        weight=1,
        patterns=[
            r".",  # Matches any non-empty string
        ]
    ),
]


# ─── Classification Function ──────────────────────────────────────────────────

def classify_intent(query: str) -> IntentClassification:
    """
    Classify a user query into an intent class.
    
    Args:
        query: User query string
    
    Returns:
        IntentClassification with intent, confidence, tools, etc.
    """
    if not query or not query.strip():
        return _default_classification()
    
    # Score all patterns
    scores: dict[str, float] = {}
    for pattern in _INTENT_PATTERNS:
        score = pattern.match_score(query)
        if score > 0:
            scores[pattern.intent] = score
    
    # Find best match
    if not scores:
        return _default_classification()
    
    best_intent = max(scores, key=scores.get)
    confidence = min(1.0, scores[best_intent])
    
    # Build classification
    return _build_classification(best_intent, confidence, query)


def _build_classification(
    intent: str,
    confidence: float,
    query: str,
) -> IntentClassification:
    """Build a classification with tools and system addendum."""
    
    # Tool subset per intent
    tool_map = {
        "build_document": {
            "tools": ["build_deck", "build_report", "read_file", "write_file"],
            "plan_first": True,
            "verify": True,
            "parallel": True,
            "addendum": (
                "You are building a document/presentation. "
                "Read all source materials first, then generate the spec, "
                "then call the builder tool. DO NOT make multiple builder calls."
            ),
        },
        "data_task": {
            "tools": ["build_sheet", "build_dashboard", "semantic_search", "read_file"],
            "plan_first": True,
            "verify": True,
            "parallel": True,
            "addendum": (
                "You are working with data. "
                "Gather data first (search/read), then transform it, "
                "then call the builder tool (sheet/dashboard)."
            ),
        },
        "code_task": {
            "tools": ["review_code", "explain_code", "fix_bug", "read_file", "bash"],
            "plan_first": False,
            "verify": True,
            "parallel": False,
            "addendum": (
                "You are analyzing or writing code. "
                "Read the relevant files, understand the context, "
                "then provide your analysis or fix. Be specific and explain your reasoning."
            ),
        },
        "research": {
            "tools": ["google_search", "bing_search", "semantic_search", "code_search"],
            "plan_first": False,
            "verify": False,
            "parallel": True,
            "addendum": (
                "You are researching. Search multiple sources in parallel. "
                "Synthesize findings. Cite sources."
            ),
        },
        "file_op": {
            "tools": ["read_file", "write_file", "edit_file", "list_directory", "bash"],
            "plan_first": False,
            "verify": True,
            "parallel": False,
            "addendum": (
                "You are manipulating files. "
                "Be careful with paths. Verify operations succeeded."
            ),
        },
        "system_cmd": {
            "tools": ["bash", "read_file"],
            "plan_first": False,
            "verify": True,
            "parallel": False,
            "addendum": (
                "You are running system commands. "
                "Read output carefully. Check exit codes. "
                "Ask for confirmation on destructive operations."
            ),
        },
        "query": {
            "tools": ["semantic_search", "google_search", "read_file"],
            "plan_first": False,
            "verify": False,
            "parallel": True,
            "addendum": (
                "Answer the user's question directly and concisely. "
                "Search if needed. Cite sources."
            ),
        },
    }
    
    # Get tool config for this intent
    config = tool_map.get(intent, tool_map["query"])
    
    # Determine rationale
    rationale_map = {
        "build_document": "You're asking to create a document/presentation",
        "data_task": "You're working with data/dashboards/spreadsheets",
        "code_task": "You're asking about code (review, debug, explain)",
        "research": "You're asking to search and research",
        "file_op": "You're asking to read/write/organize files",
        "system_cmd": "You're asking to run commands",
        "query": "General question",
    }
    rationale = rationale_map.get(intent, "General question")
    
    return IntentClassification(
        intent=intent,
        confidence=confidence,
        rationale=rationale,
        primary_tools=config["tools"],
        system_addendum=config["addendum"],
        should_plan_first=config["plan_first"],
        should_verify=config["verify"],
        parallelizable=config["parallel"],
    )


def _default_classification() -> IntentClassification:
    """Return default classification for empty/unmatched queries."""
    return IntentClassification(
        intent="query",
        confidence=0.0,
        rationale="No input or default fallback",
        primary_tools=["semantic_search", "google_search"],
        system_addendum="Answer the user's question. Search if needed.",
        should_plan_first=False,
        should_verify=False,
        parallelizable=True,
    )
