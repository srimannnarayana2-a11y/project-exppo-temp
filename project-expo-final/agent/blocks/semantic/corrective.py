"""
Corrective RAG (CRAG) — 3-path retrieval grading.

From chats L30-32:
  "CRAG grades retrieval quality and branches into three paths:
   correct (use it), incorrect (discard, search externally),
   or ambiguous (keep it but also search). Cap correction cycles
   at 3. Track correction rate: 15-30% healthy."

This sits between retrieval and synthesis:
  retriever results → grade_retrieval() → decide what to do

Three paths:
  CORRECT   → use learnings as-is, proceed to synthesis
  INCORRECT → discard learnings, re-search with refined query
  AMBIGUOUS → keep learnings + fire additional search concurrently
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from ...llm.client import NIMClient, get_client
from ...core.types import Learning

logger = logging.getLogger(__name__)

MAX_CORRECTION_CYCLES = 3


class RetrievalGrade(str, Enum):
    CORRECT = "correct"
    INCORRECT = "incorrect"
    AMBIGUOUS = "ambiguous"


@dataclass
class GradeResult:
    """Result of grading retrieval quality."""
    grade: RetrievalGrade
    confidence: float = 0.0         # 0-1 how confident the grade is
    refined_query: str = ""         # if incorrect/ambiguous, what to search next
    reason: str = ""                # why this grade was given


@dataclass
class CorrectionStats:
    """Track correction rate as a health metric."""
    total_queries: int = 0
    corrected_count: int = 0        # times we had to correct/re-search

    @property
    def correction_rate(self) -> float:
        if self.total_queries == 0:
            return 0.0
        return self.corrected_count / self.total_queries

    @property
    def health_status(self) -> str:
        """15-30% correction rate is healthy (from chats)."""
        rate = self.correction_rate
        if rate < 0.10:
            return "under_correcting"  # threshold too low
        elif rate <= 0.35:
            return "healthy"
        else:
            return "base_retrieval_broken"  # >35% means retrieval is bad


_GRADE_PROMPT = (
    "You are a retrieval quality judge. Given a query and the retrieved "
    "learnings, determine if the retrieval is:\n\n"
    "CORRECT — The learnings directly and sufficiently answer the query. "
    "The information is relevant, accurate, and covers the key aspects.\n\n"
    "INCORRECT — The learnings are irrelevant, off-topic, or clearly wrong "
    "for this query. They should be discarded entirely.\n\n"
    "AMBIGUOUS — The learnings are partially relevant but incomplete, or "
    "they're tangentially related but don't fully address the query. "
    "They're worth keeping but need supplementation.\n\n"
    "Respond with EXACTLY one line in this format:\n"
    "GRADE: correct|incorrect|ambiguous\n"
    "CONFIDENCE: 0.0-1.0\n"
    "REASON: brief explanation\n"
    "REFINED_QUERY: (only if incorrect/ambiguous) a better search query"
)


async def grade_retrieval(
    query: str,
    learnings: list[Learning],
    *,
    client: Optional[NIMClient] = None,
) -> GradeResult:
    """Grade retrieval quality using the 3-path CRAG system.

    Fast path: if no learnings, grade is INCORRECT (no LLM call needed).
    """
    # Fast path — empty results
    if not learnings:
        return GradeResult(
            grade=RetrievalGrade.INCORRECT,
            confidence=1.0,
            refined_query=query,
            reason="No learnings retrieved",
        )

    # Fast path — very high scores = likely correct
    avg_score = sum(l.score for l in learnings) / len(learnings) if learnings else 0
    if avg_score > 0.85 and len(learnings) >= 3:
        return GradeResult(
            grade=RetrievalGrade.CORRECT,
            confidence=0.9,
            reason=f"High average score ({avg_score:.2f}) with {len(learnings)} learnings",
        )

    # LLM grading for ambiguous cases
    client = client or get_client()

    learnings_text = "\n".join(
        f"- [{l.source_url or 'unknown'}] (score={l.score:.2f}) {l.text[:200]}"
        for l in learnings[:10]
    )

    messages = [
        {"role": "system", "content": _GRADE_PROMPT},
        {"role": "user", "content": f"Query: {query}\n\nRetrieved learnings:\n{learnings_text}"},
    ]

    try:
        raw = await client.chat_worker(messages, temperature=0.1, max_tokens=200)
        return _parse_grade(raw, query)
    except Exception as e:
        logger.warning("Grade LLM failed: %s, defaulting to AMBIGUOUS", e)
        return GradeResult(
            grade=RetrievalGrade.AMBIGUOUS,
            confidence=0.3,
            refined_query=query,
            reason=f"Grading failed: {e}",
        )


def _parse_grade(raw: str, original_query: str) -> GradeResult:
    """Parse the LLM's grade response."""
    lines = raw.strip().split("\n")
    grade = RetrievalGrade.AMBIGUOUS
    confidence = 0.5
    reason = ""
    refined = ""

    for line in lines:
        line = line.strip()
        if line.upper().startswith("GRADE:"):
            val = line.split(":", 1)[1].strip().lower()
            if val in ("correct", "incorrect", "ambiguous"):
                grade = RetrievalGrade(val)
        elif line.upper().startswith("CONFIDENCE:"):
            try:
                confidence = float(line.split(":", 1)[1].strip())
            except ValueError:
                pass
        elif line.upper().startswith("REASON:"):
            reason = line.split(":", 1)[1].strip()
        elif line.upper().startswith("REFINED_QUERY:"):
            refined = line.split(":", 1)[1].strip()

    return GradeResult(
        grade=grade,
        confidence=confidence,
        refined_query=refined or original_query,
        reason=reason,
    )


# Module-level stats singleton
_stats: Optional[CorrectionStats] = None


def get_correction_stats() -> CorrectionStats:
    global _stats
    if _stats is None:
        _stats = CorrectionStats()
    return _stats
