"""
Hypothesis-Driven Pivoting — the ONE shared control loop used at both:
  - per-node inside a retriever (which query to try next)
  - orchestrator-level (which subagent to trust)

GOAL → ACTION → OBSERVE → HYPOTHESIZE → DISCRIMINATE → PIVOT.

Do NOT duplicate this logic inside individual subagents or the
orchestrator — they import and call into this module.

No blind retries: on failure this goes straight to competing hypotheses
rather than trying the same action again. That's the specific failure
mode this loop exists to avoid.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from .types import Hypothesis, PivotDecision


@dataclass
class Observation:
    """What actually happened after ACTION, vs. what the goal required."""
    succeeded: bool
    result: Any = None
    detail: str = ""


# Caller-supplied callables — kept as plain functions so this loop
# stays usable whether the hypothesis step is an LLM call, a rule, or a test stub.
HypothesisGenerator = Callable[[str, Observation], list[Hypothesis]]
DiscriminatingExperiment = Callable[[Hypothesis, Hypothesis], Awaitable[Observation]]


async def run_pivot_loop(
    goal: str,
    first_action: Callable[[], Awaitable[Observation]],
    generate_hypotheses: HypothesisGenerator,
    run_discriminating_experiment: DiscriminatingExperiment,
) -> PivotDecision:
    """
    GOAL → ACTION → OBSERVE once. If that satisfies the goal, done.
    If not: HYPOTHESIZE → DISCRIMINATE → PIVOT.

    goal != method: this function never changes `goal`. It only ever
    recommends a different next_action.
    """
    observation = await first_action()
    if observation.succeeded:
        return PivotDecision(
            goal=goal,
            confirmed_hypothesis=None,
            next_action="none -- first action satisfied the goal",
        )

    hypotheses = generate_hypotheses(goal, observation)
    if not hypotheses:
        return PivotDecision(
            goal=goal,
            confirmed_hypothesis=None,
            next_action="abandon -- no hypotheses generated",
            circuit_break=[observation.detail] if observation.detail else [],
        )

    # Rank by prior, take top two
    hypotheses = sorted(hypotheses, key=lambda h: h.prior, reverse=True)
    if len(hypotheses) == 1:
        confirmed: Optional[Hypothesis] = hypotheses[0]
    else:
        h_a, h_b = hypotheses[0], hypotheses[1]
        disc_observation = await run_discriminating_experiment(h_a, h_b)
        confirmed = h_a if disc_observation.succeeded else h_b

    if confirmed is None:
        return PivotDecision(
            goal=goal,
            confirmed_hypothesis=None,
            next_action="abandon -- discrimination inconclusive",
        )

    return PivotDecision(
        goal=goal,
        confirmed_hypothesis=confirmed,
        next_action=f"pivot via {confirmed.label}: {confirmed.explanation}",
        circuit_break=[confirmed.implies_circuit_break] if confirmed.implies_circuit_break else [],
    )
