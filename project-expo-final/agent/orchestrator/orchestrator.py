"""
Lead agent: decomposes the incoming task into subagent mandates, decides
how many subagents to actually spawn, collects structured SubagentResults,
and runs core.pivot when a subagent fails.

Subagent count and firing order are NOT fixed:
  - decompose() builds a task dependency graph (TDG), not a flat list.
  - Nodes with no unresolved dependencies fire together via asyncio.gather.
  - A node with a dependency waits for its prerequisite's SubagentResult.
  - "No subagents needed" is a valid decompose() outcome.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from ..core.types import SubagentInput, SubagentResult, SubagentType, Hypothesis, PivotDecision
from ..core.pivot import Observation, run_pivot_loop
from ..core.reasoning import ThinkingProfile
from ..core.satisfaction import SatisfactionTracker
from ..llm.client import NIMClient, get_client
from ..config.budgets import DEFAULT_MAX_SUBAGENTS, FAN_OUT_MAX_SUBAGENTS

logger = logging.getLogger(__name__)


@dataclass
class TaskNode:
    """One node in the task dependency graph."""
    node_id: str
    subagent_type: SubagentType
    task: str
    depends_on: list[str] = field(default_factory=list)


@dataclass
class Decomposition:
    """Output of the decompose step. Empty nodes = handle directly."""
    nodes: list[TaskNode] = field(default_factory=list)
    fan_out_eligible: bool = False


RunSubagentFn = Callable[[SubagentInput], Awaitable[SubagentResult]]


# ── Circuit breaker registry ──

class SubagentRegistry:
    """Tracks subagent health for circuit-breaking."""

    def __init__(self, break_threshold: int = 3):
        self._failures: dict[str, int] = {}
        self._threshold = break_threshold

    def record_failure(self, subagent_name: str):
        self._failures[subagent_name] = self._failures.get(subagent_name, 0) + 1

    def record_success(self, subagent_name: str):
        self._failures[subagent_name] = 0

    def is_circuit_broken(self, subagent_type: SubagentType) -> bool:
        return self._failures.get(subagent_type.value, 0) >= self._threshold


# ── Decomposition via LLM ──

_DECOMPOSE_SYSTEM = """You are a task decomposition engine. Given a research task,
decide whether it needs to be broken into parallel sub-tasks for specialized subagents.

Available subagent types:
- "retriever": semantic web search + knowledge base retrieval
- "code_retriever": GitHub code search + AST analysis (for code implementation queries)

Rules:
- Most queries need only 1 retriever subagent. Don't over-decompose.
- Only decompose if the task has genuinely independent, parallel dimensions.
- Return empty nodes if the task should be answered directly (no retrieval needed).

Respond with ONLY a JSON object:
{
  "nodes": [
    {"node_id": "n1", "subagent_type": "retriever", "task": "specific subtask", "depends_on": []},
    {"node_id": "n2", "subagent_type": "code_retriever", "task": "specific subtask", "depends_on": []}
  ],
  "fan_out_eligible": false
}"""


async def decompose_task(
    task: str,
    gate_mode: str = "SEMANTIC",
    *,
    client: Optional[NIMClient] = None,
) -> Decomposition:
    """LLM-driven task decomposition. Returns empty nodes if no delegation needed."""
    client = client or get_client()

    # Fast-path: if gate already decided, skip LLM decomposition
    if gate_mode == "PARAMETRIC":
        return Decomposition()  # no delegation

    if gate_mode == "CODE":
        return Decomposition(nodes=[
            TaskNode("n1", SubagentType.CODE_RETRIEVER, task),
        ])

    if gate_mode == "SEMANTIC":
        return Decomposition(nodes=[
            TaskNode("n1", SubagentType.RETRIEVER, task),
        ])

    # HYBRID or complex — use LLM to decompose
    try:
        messages = [
            {"role": "system", "content": _DECOMPOSE_SYSTEM},
            {"role": "user", "content": f"Task: {task}\nGate mode: {gate_mode}"},
        ]
        raw = await client.chat_fast(messages, temperature=0.0, response_format_json=True)
        parsed = json.loads(raw)

        nodes = []
        for n in parsed.get("nodes", []):
            st = n.get("subagent_type", "retriever")
            if st not in ("retriever", "code_retriever"):
                st = "retriever"
            nodes.append(TaskNode(
                node_id=n.get("node_id", f"n{len(nodes)}"),
                subagent_type=SubagentType(st),
                task=n.get("task", task),
                depends_on=n.get("depends_on", []),
            ))

        return Decomposition(
            nodes=nodes,
            fan_out_eligible=parsed.get("fan_out_eligible", False),
        )
    except Exception as e:
        logger.warning("Decompose LLM failed: %s, using single retriever", e)
        return Decomposition(nodes=[
            TaskNode("n1", SubagentType.RETRIEVER, task),
        ])


# ── Topological layer execution ──

def _topological_layers(nodes: list[TaskNode]) -> list[list[TaskNode]]:
    """Groups nodes into layers for parallel execution."""
    remaining = {n.node_id: n for n in nodes}
    done: set[str] = set()
    layers: list[list[TaskNode]] = []

    while remaining:
        ready = [n for n in remaining.values() if all(d in done for d in n.depends_on)]
        if not ready:
            raise ValueError(f"Unresolvable dependencies: {list(remaining)}")
        layers.append(ready)
        for n in ready:
            done.add(n.node_id)
            del remaining[n.node_id]

    return layers


# ── Main orchestrator ──

async def run_orchestrator(
    task: str,
    run_subagent: RunSubagentFn,
    gate_mode: str = "SEMANTIC",
    *,
    client: Optional[NIMClient] = None,
    registry: Optional[SubagentRegistry] = None,
    max_subagents: int = DEFAULT_MAX_SUBAGENTS,
) -> dict[str, SubagentResult]:
    """
    Entry point. Returns node_id → SubagentResult for every node that ran.
    Empty dict = no delegation needed (caller should answer directly).
    """
    registry = registry or SubagentRegistry()
    client = client or get_client()

    decomposition = await decompose_task(task, gate_mode, client=client)
    if not decomposition.nodes:
        return {}

    effective_max = FAN_OUT_MAX_SUBAGENTS if decomposition.fan_out_eligible else max_subagents
    nodes = decomposition.nodes[:effective_max]
    layers = _topological_layers(nodes)
    results: dict[str, SubagentResult] = {}

    for layer in layers:
        async def _dispatch(node: TaskNode) -> tuple[str, SubagentResult]:
            if registry.is_circuit_broken(node.subagent_type):
                return node.node_id, SubagentResult(
                    subagent_type=node.subagent_type,
                    success=False,
                    error_reason="circuit_broken",
                )

            # Fold upstream results into payload
            upstream = {dep: results[dep] for dep in node.depends_on if dep in results}
            sub_input = SubagentInput(
                task=node.task,
                subagent_type=node.subagent_type,
                payload={"upstream": upstream} if upstream else {},
            )

            # Execute with pivot loop for failure recovery
            last_result = [None]

            async def first_action():
                last_result[0] = await run_subagent(sub_input)
                return Observation(
                    succeeded=last_result[0].success,
                    detail=last_result[0].error_reason,
                )

            def gen_hypotheses(_goal, _obs):
                return [
                    Hypothesis("H_transient", "transient failure — retry may work", prior=0.5),
                    Hypothesis(
                        "H_wrong_subagent", "wrong subagent type for this subtask",
                        prior=0.5, implies_circuit_break=node.subagent_type.value,
                    ),
                ]

            async def discriminate(_h_a, _h_b):
                last_result[0] = await run_subagent(sub_input)
                return Observation(
                    succeeded=last_result[0].success,
                    detail=last_result[0].error_reason,
                )

            decision = await run_pivot_loop(
                goal=node.task,
                first_action=first_action,
                generate_hypotheses=gen_hypotheses,
                run_discriminating_experiment=discriminate,
            )

            for name in decision.circuit_break:
                registry.record_failure(name)
            if last_result[0] and last_result[0].success:
                registry.record_success(node.subagent_type.value)

            return node.node_id, last_result[0] or SubagentResult(
                subagent_type=node.subagent_type,
                success=False,
                error_reason=decision.next_action,
            )

        layer_results = await asyncio.gather(*(_dispatch(n) for n in layer))
        for node_id, result in layer_results:
            results[node_id] = result

    return results
