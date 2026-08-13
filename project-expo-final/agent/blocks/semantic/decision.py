"""
Per-node decision LLM — the brain of each recursive node.

Given what was retrieved, decides:
  - Is this sufficient to answer the sub-goal? (EIG check)
  - If not: what queries would most reduce remaining uncertainty?
  - Is the gap code-shaped (needs code_retriever) vs semantic?
  - ONLY at depth ceiling: is this find valuable enough for extension?

Anti-sycophancy: calls twice at temp>0, only accepts sufficient=True
if both agree. This directly targets the documented failure mode where
a single LLM pass confidently reports "sufficient" when it isn't.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from ...llm.client import NIMClient, get_client
from ...cache.llm_cache import LLMCache, get_llm_cache
from .types import Chunk, Decision, Mode

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are the decision engine for one node of a recursive research retriever.

Given the original query, retrieved chunks, current depth, and max depth, decide:

1. SUFFICIENT: Is the retrieved information enough to answer this sub-goal?
   - "sufficient" means: adding more search would NOT meaningfully change the answer
   - Be honest: if the chunks are tangential or shallow, say insufficient
   
2. NEXT_QUERIES: If not sufficient, generate 2-3 follow-up queries that would
   MOST reduce remaining uncertainty. These should be specific and non-overlapping.
   Think: "which question eliminates the most possibilities?" (EIG principle)

3. NEEDS_CODE: Is the gap code-shaped? (needs implementation examples, not docs)

4. REQUEST_EXTENSION: ONLY if depth == max_depth AND what you just found is
   unusually high-value (a concrete lead, not just "could dig more"). This
   should fire RARELY.

Respond with ONLY a JSON object:
{
  "sufficient": true/false,
  "reason": "one sentence",
  "next_queries": ["query1", "query2"],
  "next_mode": null or "public" or "kb",
  "needs_code_retriever": false,
  "request_extension": false,
  "extension_justification": ""
}"""


def _parse_decision(raw: str) -> dict:
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        # Fail toward "not sufficient" — safer to search more than to stop early
        return {
            "sufficient": False,
            "reason": "decision_parse_error",
            "next_queries": [],
        }


async def decision_llm(
    query: str,
    mode: Mode,
    reranked_chunks: list[Chunk],
    depth: int,
    max_depth: int,
    *,
    client: Optional[NIMClient] = None,
    cache: Optional[LLMCache] = None,
) -> Decision:
    """
    Per-node decision with anti-sycophancy self-consistency check.

    At depth < max_depth: single call (speed matters more than safety).
    At depth == max_depth: two calls at temp>0, only accept sufficient=True
    if both agree (prevents premature termination at the boundary).
    """
    client = client or get_client()
    cache = cache or get_llm_cache()

    # Build context from chunks
    chunks_text = "\n\n".join(
        f"[{i}] ({c.source_url}): {c.text[:500]}"
        for i, c in enumerate(reranked_chunks[:8])
    ) or "(no chunks retrieved)"

    user_msg = (
        f"Query: {query}\n"
        f"Mode: {mode.value}\n"
        f"Depth: {depth}/{max_depth}\n"
        f"Retrieved chunks:\n{chunks_text}"
    )

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]

    # Check cache
    cache_key_prompt = f"decision|{user_msg}"
    cached = cache.get(cache_key_prompt, "decision_llm", _SYSTEM_PROMPT)
    if cached:
        parsed = _parse_decision(cached)
        return _to_decision(parsed)

    # At depth boundary: self-consistency check (2 calls, temp>0)
    at_boundary = depth >= max_depth

    if at_boundary:
        raw1 = await client.chat_fast(messages, temperature=0.3, response_format_json=True)
        raw2 = await client.chat_fast(messages, temperature=0.3, response_format_json=True)
        parsed1 = _parse_decision(raw1)
        parsed2 = _parse_decision(raw2)

        # Only accept sufficient=True if BOTH agree
        if parsed1.get("sufficient") and parsed2.get("sufficient"):
            result = parsed1
        elif not parsed1.get("sufficient") and not parsed2.get("sufficient"):
            # Both say insufficient — merge their next_queries
            q1 = parsed1.get("next_queries", [])
            q2 = parsed2.get("next_queries", [])
            merged_queries = list(dict.fromkeys(q1 + q2))[:3]
            result = parsed1
            result["next_queries"] = merged_queries
        else:
            # Disagreement — fail toward "not sufficient"
            not_sufficient = parsed1 if not parsed1.get("sufficient") else parsed2
            result = not_sufficient
            result["sufficient"] = False
            result["reason"] = "self_consistency_disagreement"
    else:
        raw = await client.chat_fast(messages, temperature=0.0, response_format_json=True)
        result = _parse_decision(raw)

    # Cache the result
    cache.set(cache_key_prompt, "decision_llm", json.dumps(result), _SYSTEM_PROMPT)

    return _to_decision(result)


def _to_decision(parsed: dict) -> Decision:
    """Convert parsed JSON to Decision dataclass."""
    next_mode = parsed.get("next_mode")
    if next_mode and next_mode in ("public", "kb"):
        next_mode = Mode(next_mode)
    else:
        next_mode = None

    return Decision(
        sufficient=parsed.get("sufficient", False),
        reason=parsed.get("reason", ""),
        next_queries=parsed.get("next_queries", [])[:3],
        next_mode=next_mode,
        needs_code_retriever=parsed.get("needs_code_retriever", False),
        request_extension=parsed.get("request_extension", False),
        extension_justification=parsed.get("extension_justification", ""),
    )
