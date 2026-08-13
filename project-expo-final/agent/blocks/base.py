"""
Block adapters — bridge block-internal types (BlockInput/NodeResult) to
orchestrator-level contracts (SubagentInput/SubagentResult).

New subagent types add an adapter here and register in SUBAGENT_DISPATCH.
The orchestrator never needs to know each block's real call signature.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

from ..core.types import SubagentInput, SubagentResult, SubagentType, Learning

logger = logging.getLogger(__name__)


async def run_retriever_subagent(sub_input: SubagentInput) -> SubagentResult:
    """Adapts the recursive semantic retriever block."""
    from .semantic.block import semantic_retriever_block, collect_tree
    from .semantic.types import BlockInput, Mode

    try:
        mode_value = sub_input.payload.get("mode", Mode.PUBLIC.value)
        block_input = BlockInput(
            query=sub_input.payload.get("query", sub_input.task),
            url=sub_input.payload.get("url"),
            mode=Mode(mode_value),
            fetch_fn=sub_input.payload.get("fetch_fn"),
            code_tool_fn=sub_input.payload.get("code_tool_fn"),
        )
        node_result = await semantic_retriever_block(block_input)
    except Exception as exc:
        return SubagentResult(
            subagent_type=SubagentType.RETRIEVER,
            success=False,
            error_reason=f"retriever_exception: {exc}",
            session_id=sub_input.session_id,
            turn_id=sub_input.turn_id,
            parent_id=sub_input.parent_id,
        )

    learnings_raw, source_urls = collect_tree(node_result)
    learnings = [Learning(text=l.text, source_url=l.source_url, score=l.score) for l in learnings_raw]
    success = bool(learnings) and node_result.terminated_reason not in ("timeout_or_error", "no_results")

    return SubagentResult(
        subagent_type=SubagentType.RETRIEVER,
        success=success,
        learnings=learnings,
        source_urls=source_urls,
        error_reason="" if success else node_result.terminated_reason,
        session_id=sub_input.session_id,
        turn_id=sub_input.turn_id,
        parent_id=sub_input.parent_id,
    )


async def run_code_retriever_subagent(sub_input: SubagentInput) -> SubagentResult:
    """Adapts the code retriever stub. Returns clean failure rather than
    letting NotImplementedError propagate."""
    from .code.block import code_retriever_block

    try:
        result = await code_retriever_block(
            sub_input.task,
            code_tool_fn=sub_input.payload.get("code_tool_fn"),
        )
        learnings = getattr(result, "learnings", [])
        source_urls = getattr(result, "source_urls", [])
        return SubagentResult(
            subagent_type=SubagentType.CODE_RETRIEVER,
            success=True,
            learnings=[Learning(text=l) if isinstance(l, str) else l for l in learnings],
            source_urls=source_urls,
            session_id=sub_input.session_id,
            turn_id=sub_input.turn_id,
            parent_id=sub_input.parent_id,
        )
    except NotImplementedError:
        return SubagentResult(
            subagent_type=SubagentType.CODE_RETRIEVER,
            success=False,
            error_reason="code_retriever_not_implemented",
            session_id=sub_input.session_id,
            turn_id=sub_input.turn_id,
            parent_id=sub_input.parent_id,
        )
    except Exception as exc:
        return SubagentResult(
            subagent_type=SubagentType.CODE_RETRIEVER,
            success=False,
            error_reason=f"code_retriever_exception: {exc}",
            session_id=sub_input.session_id,
            turn_id=sub_input.turn_id,
            parent_id=sub_input.parent_id,
        )


async def run_sandbox_subagent(sub_input: SubagentInput) -> SubagentResult:
    """Adapts the sandbox block for file creation tasks."""
    from .sandbox.block import sandbox_block, SandboxInput

    try:
        sandbox_input = SandboxInput(
            task=sub_input.task,
            sandbox_dir=sub_input.payload.get("sandbox_dir", ""),
            sandbox_fn=sub_input.payload.get("sandbox_fn"),
            context=sub_input.payload.get("context", ""),
        )
        result = await sandbox_block(sandbox_input)

        # Convert file list to learnings for orchestrator
        learnings = []
        if result.file_tree:
            learnings.append(Learning(
                text=f"Created project: {result.file_tree.overview}\n"
                     f"Files: {', '.join(result.files_created)}\n"
                     f"Entry point: {result.file_tree.entry_point}",
            ))
        for f in result.files_created:
            learnings.append(Learning(text=f"Created file: {f}"))

        return SubagentResult(
            subagent_type=SubagentType.SANDBOX,
            success=result.success,
            learnings=learnings,
            source_urls=[],
            error_reason="" if result.success else "; ".join(result.errors),
            session_id=sub_input.session_id,
            turn_id=sub_input.turn_id,
            parent_id=sub_input.parent_id,
        )
    except Exception as exc:
        return SubagentResult(
            subagent_type=SubagentType.SANDBOX,
            success=False,
            error_reason=f"sandbox_exception: {exc}",
            session_id=sub_input.session_id,
            turn_id=sub_input.turn_id,
            parent_id=sub_input.parent_id,
        )


SUBAGENT_DISPATCH: dict[SubagentType, Callable] = {
    SubagentType.RETRIEVER: run_retriever_subagent,
    SubagentType.CODE_RETRIEVER: run_code_retriever_subagent,
    SubagentType.SANDBOX: run_sandbox_subagent,
}


async def run_subagent(sub_input: SubagentInput) -> SubagentResult:
    """Single entry point — dispatches by subagent_type."""
    handler = SUBAGENT_DISPATCH.get(sub_input.subagent_type)
    if handler is None:
        return SubagentResult(
            subagent_type=sub_input.subagent_type,
            success=False,
            error_reason=f"unknown_subagent_type: {sub_input.subagent_type}",
        )
    return await handler(sub_input)
