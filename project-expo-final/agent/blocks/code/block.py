"""
Code Retriever Block — STUB with injectable seam.

This wraps the existing github_researchtool.py as the internal engine.
The orchestrator calls this through the SubagentInput/SubagentResult
contract; this adapter handles the translation.

If code_tool_fn is injected (user's custom code retriever), it takes
priority over the built-in github_researchtool. This is the seam.

For now, if no code_tool_fn is provided and github_researchtool isn't
importable, this raises NotImplementedError — the adapter in blocks/base.py
catches it and returns a clean SubagentResult failure.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from ..semantic.types import NodeResult

logger = logging.getLogger(__name__)


async def code_retriever_block(
    query: str,
    *,
    code_tool_fn: Optional[Callable] = None,
    keywords: Optional[list[str]] = None,
    language: Optional[str] = None,
) -> NodeResult:
    """
    Code retriever block entry point.

    Priority:
    1. Injected code_tool_fn (user's custom tool)
    2. github_researchtool.py (if importable)
    3. NotImplementedError (caught by adapter)
    """
    # Priority 1: User's custom code tool
    if code_tool_fn is not None:
        try:
            result = await code_tool_fn(query)
            if isinstance(result, NodeResult):
                return result
            # If it returns a dict (like github_researchtool), adapt it
            if isinstance(result, dict):
                chunks = result.get("chunks", [])
                learnings = [c.get("content", "") for c in chunks if c.get("content")]
                source_urls = [c.get("repo", "") + "/" + c.get("path", "") for c in chunks]
                return NodeResult(
                    query=query,
                    learnings=learnings,
                    source_urls=source_urls,
                    terminated_reason="leaf_answered",
                )
        except Exception as e:
            logger.warning("Custom code_tool_fn failed: %s", e)

    # Priority 2: Try importing github_researchtool
    try:
        import sys
        import os

        # Add the parent directory of github_researchtool.py to path
        tool_dir = os.path.join(os.path.dirname(__file__), "..", "..", "..")
        if tool_dir not in sys.path:
            sys.path.insert(0, os.path.abspath(tool_dir))

        from github_researchtool import code_retriever_tool

        result = await code_retriever_tool(
            query=query,
            keywords=keywords or [],
            language=language,
            deep_search=True,
        )

        chunks = result.get("chunks", [])
        learnings = [c.get("content", "") for c in chunks if c.get("content")]
        source_urls = [
            f"{c.get('repo', '')}/{c.get('path', '')}" for c in chunks
        ]

        return NodeResult(
            query=query,
            learnings=learnings,
            source_urls=source_urls,
            terminated_reason="leaf_answered" if learnings else "no_results",
        )
    except ImportError:
        logger.info("github_researchtool.py not found, code retriever unavailable")
    except Exception as e:
        logger.warning("github_researchtool failed: %s", e)

    # Priority 3: Not implemented
    raise NotImplementedError(
        "Code retriever block: no code_tool_fn injected and "
        "github_researchtool.py not importable"
    )
