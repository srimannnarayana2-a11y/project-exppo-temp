"""
EIG-scored clarifying question generation.

Memory-gated: asking is the FALLBACK, not the default. If memory_context
already resolves the ambiguity, this resolves silently instead of asking.

Runs as its own track, parallel to retrieval by default — only sequenced
against retrieval when the search target itself depends on the answer.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional

from .client import NIMClient, get_client
from ..config.budgets import CLARIFY_TEMPERATURE

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """Given a query and any known context about this user \
(which may be empty), decide what to do about ambiguity in the query.

Ambiguity means: the query could reasonably be answered several different, \
meaningfully different ways, and picking wrong would produce an answer the \
person didn't want.

Decision order, in priority:
1. If there's no real ambiguity: should_ask=false, resolved_by_memory=null.
2. If there's ambiguity AND the known context resolves it: should_ask=false, \
resolved_by_memory=<a short phrase describing the resolved direction>.
3. Only if real ambiguity AND context doesn't resolve it: should_ask=true, \
with the single highest-information-gain question. Never more than one question.

Respond with ONLY a JSON object with keys: "should_ask" (bool), \
"question" (string, "" if should_ask is false), "depends_on_search_target" \
(bool -- true if the answer would change what should be searched for), \
"resolved_by_memory" (string or null)."""


@dataclass
class ClarifyDecision:
    should_ask: bool
    question: str
    depends_on_search_target: bool
    resolved_by_memory: Optional[str] = None


async def generate_clarifying_question(
    query: str,
    *,
    client: Optional[NIMClient] = None,
    memory_context: Optional[list[str]] = None,
) -> ClarifyDecision:
    client = client or get_client()
    context_block = "\n".join(f"- {c}" for c in memory_context) if memory_context else "(none known)"
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": f"Query: {query}\n\nKnown context about this user:\n{context_block}"},
    ]

    try:
        raw = await client.chat_worker(
            messages, temperature=CLARIFY_TEMPERATURE, response_format_json=True,
        )
        parsed = _parse(raw)
        return ClarifyDecision(
            should_ask=parsed.get("should_ask", False),
            question=parsed.get("question", ""),
            depends_on_search_target=parsed.get("depends_on_search_target", False),
            resolved_by_memory=parsed.get("resolved_by_memory"),
        )
    except Exception as e:
        logger.warning("Clarify LLM failed: %s", e)
        return ClarifyDecision(should_ask=False, question="", depends_on_search_target=False)


def _parse(raw: str) -> dict:
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"should_ask": False, "question": "", "depends_on_search_target": False}
