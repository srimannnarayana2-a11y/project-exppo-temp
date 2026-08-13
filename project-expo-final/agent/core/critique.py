"""
Multi-perspective critique — for reviewing something that "succeeded"
but might not actually be good.

Two hard constraints from the research:

1. GENUINELY DIFFERENT ROLES — not N copies of "please be critical."
   Undifferentiated multi-agent debate actively DECREASES accuracy as
   agents drift toward agreement under peer pressure.

2. ISOLATED calls — each persona gets ONLY the artifact and the goal,
   never the generation's own reasoning trace.

CRITICAL CAVEAT: unanimous approval across all four personas is NOT
proof of correctness. Where a more objective check exists (run the tests,
verify against a real source), USE IT alongside this.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Optional

from ..llm.client import NIMClient, get_client
from ..config.budgets import CRITIQUE_TEMPERATURE

logger = logging.getLogger(__name__)


@dataclass
class CritiquePersona:
    name: str
    system_prompt: str


_BRUTAL_CRITIC = CritiquePersona(
    name="brutal_critic",
    system_prompt=(
        "You are reviewing a piece of work against the goal it was meant to "
        "achieve. Find what is actually wrong with it — concrete errors, "
        "inconsistencies, unsupported claims, gaps. Do not soften findings, "
        "do not open with something positive first. If there is nothing wrong, "
        "say so plainly — do not manufacture a criticism to seem thorough."
    ),
)

_BRUTAL_EXPECTATIONIST = CritiquePersona(
    name="brutal_expectationist",
    system_prompt=(
        "You represent the highest reasonable bar for this work. Would someone "
        "genuinely expert in this domain call this excellent, or merely adequate? "
        "What is the gap between what was delivered and what would actually "
        "impress someone who knows this space well? Be specific about the gap."
    ),
)

_BRUTAL_REALIST = CritiquePersona(
    name="brutal_realist",
    system_prompt=(
        "Evaluate whether this actually works, not whether the ambition behind "
        "it is good. Are the claims actually true? Given real constraints, will "
        "this approach actually function as described? Do not evaluate the idea "
        "— evaluate this specific execution of it."
    ),
)

_OVERTHINKER = CritiquePersona(
    name="overthinker",
    system_prompt=(
        "Assume this WILL fail. Your only job is finding how. Edge cases, "
        "race conditions, what happens when an assumption this relies on turns "
        "out false, what breaks under scale or adversarial input. Be paranoid "
        "and specific — a concrete failure scenario, not a vague concern."
    ),
)

ALL_PERSONAS = [_BRUTAL_CRITIC, _BRUTAL_EXPECTATIONIST, _BRUTAL_REALIST, _OVERTHINKER]


@dataclass
class CritiqueResult:
    persona_name: str
    verdict: str              # "approve" | "reject" | "approve_with_concerns"
    concerns: list[str] = field(default_factory=list)


@dataclass
class MultiCritiqueResult:
    results: list[CritiqueResult]
    consensus: bool           # True only if every persona approved outright
    disagreement: list[str] = field(default_factory=list)


def _parse(raw: str) -> dict:
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"verdict": "reject", "concerns": ["critique response was not valid JSON"]}


async def _run_one_persona(
    persona: CritiquePersona, goal: str, artifact: str, client: NIMClient,
) -> CritiqueResult:
    messages = [
        {"role": "system", "content": persona.system_prompt + (
            '\n\nRespond with ONLY a JSON object: {"verdict": "approve" | '
            '"reject" | "approve_with_concerns", "concerns": [list of strings]}'
        )},
        {"role": "user", "content": f"Goal: {goal}\n\nArtifact to review:\n{artifact}"},
    ]
    raw = await client.chat_worker(messages, temperature=CRITIQUE_TEMPERATURE, response_format_json=True)
    parsed = _parse(raw)
    return CritiqueResult(
        persona_name=persona.name,
        verdict=parsed.get("verdict", "reject"),
        concerns=parsed.get("concerns", []),
    )


async def run_multi_critique(
    goal: str,
    artifact: str,
    *,
    client: Optional[NIMClient] = None,
    personas: Optional[list[CritiquePersona]] = None,
) -> MultiCritiqueResult:
    """
    Runs every persona as an independent, isolated call — true parallel
    independence, same reason the orchestrator fires independent layers
    concurrently rather than in sequence.
    """
    client = client or get_client()
    personas = personas or ALL_PERSONAS

    results = await asyncio.gather(
        *(_run_one_persona(p, goal, artifact, client) for p in personas)
    )

    consensus = all(r.verdict == "approve" for r in results)
    disagreement = [
        f"{r.persona_name}: {r.verdict} -- {'; '.join(r.concerns)}"
        if r.concerns else f"{r.persona_name}: {r.verdict}"
        for r in results if r.verdict != "approve"
    ]

    return MultiCritiqueResult(
        results=list(results),
        consensus=consensus,
        disagreement=disagreement,
    )
