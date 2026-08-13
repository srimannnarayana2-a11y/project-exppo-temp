"""
Global synthesis LLM — the single accumulator over all leaf learnings.

KEY PHILOSOPHY from chats:
  1. NOT a flat list — connect related facts into narrative
  2. Hybrid ordering: chronological/logical structure FIRST, then
     flag genuinely surprising facts within that structure
  3. DOB/age redundancy: prefer fundamental facts over derivable ones
     (if you know DOB, don't separately state age — it's derivable)
  4. Fill gaps: if learnings have holes, the LLM can use its own
     knowledge to bridge them, but MUST flag what's from retrieval
     vs what's from its own knowledge
  5. Presentation quality matters — this is what the user sees

Two delivery modes:
  - Non-streaming: returns full answer at once
  - Streaming: yields token deltas (for WebSocket/SSE)
"""

from __future__ import annotations

import logging
from typing import AsyncIterator, Optional

from .client import NIMClient, get_client
from .persona import build_persona_prompt
from ..core.types import Learning

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are the final synthesis step of a recursive research agent. You "
    "receive the original query and every learning gathered across all "
    "search branches, tagged with sources and ordered by relevance.\n\n"
    "Your job is to write ONE coherent, well-structured answer that "
    "directly addresses the query. Follow these principles:\n\n"
    "1. NARRATIVE OVER LIST: Connect related facts into a real story. "
    "If several learnings relate to the same event, entity, or timeline, "
    "present them together in logical/chronological order — not as "
    "disconnected bullet points.\n\n"
    "2. HYBRID SURPRISAL: Use a logical structure as the backbone, but "
    "explicitly flag genuinely surprising or counterintuitive findings "
    "within that structure. Don't lead with ALL the surprises and don't "
    "bury them — weave them naturally where they fit chronologically or "
    "logically, but call them out (e.g., 'Unexpectedly, ...' or "
    "'Contrary to common belief, ...').\n\n"
    "3. REDUNDANCY ELIMINATION: Prefer fundamental facts over derivable "
    "ones. If you state a date of birth, don't separately state the age "
    "— it's derivable. If you explain a mechanism, don't also restate "
    "the obvious consequence. Every sentence should add NEW information.\n\n"
    "4. GAP BRIDGING: If the learnings have gaps that your own knowledge "
    "can fill to make the narrative coherent, you may do so, but you "
    "MUST mark what came from the retrieved learnings vs your own "
    "knowledge (e.g., 'Based on the retrieved sources...' vs "
    "'From general knowledge...'). Never present your own knowledge "
    "as if it came from the sources.\n\n"
    "5. DEPTH MATCHING: Match the depth and technicality of your answer "
    "to what the query itself signals. A detailed technical prompt "
    "deserves a detailed technical answer. A casual question gets a "
    "concise, accessible answer. Don't over-explain to experts or "
    "under-explain to beginners.\n\n"
    "6. SOURCE ATTRIBUTION: When making specific factual claims, "
    "attribute them to their source where possible. This builds trust "
    "and lets the user verify."
)


def _build_messages(
    query: str,
    learnings: list[Learning],
    prompt_specificity: str = "standard",
) -> list[dict]:
    """Build the synthesis prompt. Mechanical ordering by score,
    with prompt-specificity hint for depth calibration."""
    ordered = sorted(learnings, key=lambda l: l.score, reverse=True)
    learnings_block = "\n\n".join(
        f"- [{l.source_url or 'source unknown'}] {l.text}" for l in ordered
    ) or "(no learnings gathered)"

    specificity_hint = ""
    if prompt_specificity == "expert":
        specificity_hint = "\n\nNote: This query appears to be from someone with domain expertise. Provide deep technical detail."
    elif prompt_specificity == "casual":
        specificity_hint = "\n\nNote: This query appears to be a casual/general question. Be concise and accessible."

    # Jarvis persona + synthesis rules combined
    persona = build_persona_prompt(prompt_specificity)
    full_system = persona + "\n\n--- SYNTHESIS RULES ---\n\n" + _SYSTEM_PROMPT

    return [
        {"role": "system", "content": full_system},
        {"role": "user", "content": f"Query: {query}{specificity_hint}\n\nLearnings:\n{learnings_block}"},
    ]


async def global_synthesis_llm(
    query: str,
    learnings: list[Learning],
    *,
    client: Optional[NIMClient] = None,
    prompt_specificity: str = "standard",
) -> str:
    """Non-streaming — returns the full answer at once."""
    client = client or get_client()
    return await client.chat(
        _build_messages(query, learnings, prompt_specificity),
        temperature=0.2,
        max_tokens=2048,
    )


async def global_synthesis_llm_stream(
    query: str,
    learnings: list[Learning],
    *,
    client: Optional[NIMClient] = None,
    prompt_specificity: str = "standard",
) -> AsyncIterator[str]:
    """Streaming — yields text deltas."""
    client = client or get_client()
    async for delta in client.chat_stream(
        _build_messages(query, learnings, prompt_specificity),
        temperature=0.2,
        max_tokens=2048,
    ):
        yield delta


async def direct_answer_llm(
    query: str,
    *,
    client: Optional[NIMClient] = None,
    prompt_specificity: str = "standard",
) -> str:
    """No-retrieval path. Entry_gate decided no grounding needed."""
    client = client or get_client()

    specificity_hint = ""
    if prompt_specificity == "expert":
        specificity_hint = " Provide deep technical detail appropriate for a domain expert."
    elif prompt_specificity == "casual":
        specificity_hint = " Be concise and accessible."

    return await client.chat(
        [{"role": "user", "content": query + specificity_hint}],
        temperature=0.3,
        max_tokens=1024,
    )


async def direct_answer_llm_stream(
    query: str,
    *,
    client: Optional[NIMClient] = None,
    prompt_specificity: str = "standard",
) -> AsyncIterator[str]:
    """Streaming direct answer — no retrieval path."""
    client = client or get_client()

    specificity_hint = ""
    if prompt_specificity == "expert":
        specificity_hint = " Provide deep technical detail appropriate for a domain expert."
    elif prompt_specificity == "casual":
        specificity_hint = " Be concise and accessible."

    async for delta in client.chat_stream(
        [{"role": "user", "content": query + specificity_hint}],
        temperature=0.3,
        max_tokens=1024,
    ):
        yield delta
