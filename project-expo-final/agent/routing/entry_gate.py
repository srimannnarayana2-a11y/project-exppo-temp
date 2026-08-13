"""
Entry gate — the original "hallucination threshold."

Decides: answer directly from parametric knowledge, or retrieve first?
Combines regex fast-path (0ms for 90% of queries) from github_researchtool.py
with LLM fallback for ambiguous cases.

Six-way classification:
  - PARAMETRIC: stable algorithms, established concepts — no retrieval
  - SEMANTIC: needs conceptual docs, articles — semantic retrieval
  - CODE: needs real-world implementation examples — code retrieval
  - HYBRID: needs both code + conceptual grounding
  - SKILL: matches a Jarvis skill (deck builder, report builder, etc.)
  - URL_DIRECT: user gave a URL directly — skip search, fetch directly

Fails TOWARD retrieval on ambiguity — unnecessary search costs latency,
a skipped necessary one costs a hallucinated answer.
"""

from __future__ import annotations

import json
import re
import logging
from dataclasses import dataclass
from typing import Optional

from ..llm.client import NIMClient, get_client
from ..cache.llm_cache import LLMCache, get_llm_cache

logger = logging.getLogger(__name__)


@dataclass
class GateDecision:
    needs_retrieval: bool
    mode: str              # "PARAMETRIC" | "SEMANTIC" | "CODE" | "HYBRID"
    reason: str


# URL detection — user gave a direct URL to process
_URL_PATTERN = re.compile(
    r"https?://[^\s]+",
    re.IGNORECASE,
)

_LLM_SYSTEM_TEMPLATE = """Decide how to route a user query. Respond with ONLY a JSON object:

- "needs_retrieval": true/false
- "mode": "PARAMETRIC" | "SEMANTIC" | "CODE" | "HYBRID" | "SKILL" | "URL_DIRECT"
- "reason": one short phrase

Classification rules:
- PARAMETRIC: Standard algorithms, language syntax, common patterns, math/logic. LLM already knows these.
- SEMANTIC: Needs conceptual explanation, documentation, research papers, current events.
- CODE: Needs real-world code examples, niche libraries, domain-specific tools.
- HYBRID: Needs both code examples AND conceptual grounding.
- SKILL: User wants to CREATE/GENERATE a deliverable (presentation, report, website, analysis, review). Available skills: {skills}
- URL_DIRECT: User provided a specific URL to fetch/scrape/read.

IMPORTANT: If the user asks to "create", "make", "build", "generate" a document/presentation/website/report — use SKILL mode."""


def _build_llm_prompt() -> str:
    """Build gate LLM prompt dynamically with available skill names."""
    try:
        from ..skills.registry import get_skill_registry
        registry = get_skill_registry()
        skills_str = ", ".join(registry.skill_names) if registry.skill_names else "none registered"
    except Exception:
        skills_str = "deck_builder, report_builder, website_builder, data_analyzer, code_reviewer"
    return _LLM_SYSTEM_TEMPLATE.format(skills=skills_str)


async def entry_gate(
    query: str,
    *,
    client: Optional[NIMClient] = None,
    cache: Optional[LLMCache] = None,
) -> GateDecision:
    """Classify query complexity. Uses URL/Skill fast-paths, then LLM semantic routing."""

    # ── URL direct fast-path (FIRST — most specific) ──
    url_match = _URL_PATTERN.search(query)
    if url_match:
        return GateDecision(
            needs_retrieval=True,
            mode="URL_DIRECT",
            reason=f"Direct URL detected: {url_match.group()[:60]}",
        )

    # ── Skill matching (before LLM — user wants deliverable) ──
    try:
        from ..skills.registry import get_skill_registry
        registry = get_skill_registry()
        skill_match = registry.match(query)
        if skill_match and skill_match.score >= 0.15:
            return GateDecision(
                needs_retrieval=False,
                mode="SKILL",
                reason=f"Skill matched: {skill_match.skill.name} (score={skill_match.score:.2f})",
            )
    except Exception as e:
        logger.debug("Skill matching failed: %s", e)

    # ── LLM fallback for ambiguous queries (dynamic, not hardcoded) ──
    client = client or get_client()
    cache = cache or get_llm_cache()

    system_prompt = _build_llm_prompt()
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": query},
    ]

    # Check cache
    cached = cache.get(query, "entry_gate", system_prompt)
    if cached:
        parsed = _parse(cached)
        return _to_decision(parsed)

    try:
        raw = await client.chat_fast(
            messages, temperature=0.0, response_format_json=True, max_tokens=100,
        )
        cache.set(query, "entry_gate", raw, system_prompt)
        parsed = _parse(raw)
        return _to_decision(parsed)
    except Exception as e:
        logger.warning("Entry gate LLM failed: %s, defaulting to retrieval", e)
        return GateDecision(
            needs_retrieval=True,
            mode="SEMANTIC",
            reason="gate_llm_failed_safe",
        )


def _parse(raw: str) -> dict:
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"needs_retrieval": True, "mode": "SEMANTIC", "reason": "parse_error_failed_safe"}


def _to_decision(parsed: dict) -> GateDecision:
    mode = parsed.get("mode", "SEMANTIC")
    if mode not in ("PARAMETRIC", "SEMANTIC", "CODE", "HYBRID", "SKILL", "URL_DIRECT"):
        mode = "SEMANTIC"
    return GateDecision(
        needs_retrieval=parsed.get("needs_retrieval", True),
        mode=mode,
        reason=parsed.get("reason", ""),
    )
