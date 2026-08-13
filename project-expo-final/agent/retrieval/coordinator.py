"""
Retriever Coordinator — Adaptive Semantic + Code Retriever Integration.

Orchestrates semantic and code retrievers with dynamic switching.
When semantic retrieval identifies code gaps (needs_code_retriever=True),
automatically spawns code retriever and merges results.

This maintains backward compatibility with the existing orchestrator while
adding intelligent adaptive retrieval behavior.
"""

from __future__ import annotations

import logging
import asyncio
from dataclasses import dataclass, field
from typing import Optional, Callable, Awaitable

from ..core.types import Learning, SubagentInput, SubagentType
from ..core.reasoning import ThinkingProfile
from ..core.satisfaction import SatisfactionTracker
from ..llm.client import NIMClient, get_client

logger = logging.getLogger(__name__)


@dataclass
class CoordinatedRetrievalResult:
    """Result from coordinated semantic + code retrieval."""
    success: bool
    learnings: list[Learning] = field(default_factory=list)
    source_urls: list[str] = field(default_factory=list)
    error_reason: str = ""
    used_code_retriever: bool = False
    used_semantic_retriever: bool = False
    depth_reached: int = 0
    message: str = ""  # Human-readable summary


class RetrieverCoordinator:
    """
    Coordinates semantic and code retrievers.
    
    Purpose:
      - Primary: semantic retrieval (docs, concepts, context)
      - Watch: decision_llm flag "needs_code_retriever"
      - If True: spawn code_retriever dynamically
      - Merge: combine semantic + code results
      - Repeat: until information gain threshold met or depth exceeded
    
    This is a wrapper around existing retriever infrastructure that adds
    coordination logic without modifying the underlying blocks.
    """

    def __init__(
        self,
        semantic_retriever_fn: Callable[[SubagentInput], Awaitable],
        code_retriever_fn: Callable[[SubagentInput], Awaitable],
        client: Optional[NIMClient] = None,
    ):
        """
        Initialize coordinator.
        
        Args:
            semantic_retriever_fn: Function that executes semantic retrieval
            code_retriever_fn: Function that executes code retrieval
            client: LLM client for decision-making
        """
        self.semantic_retriever_fn = semantic_retriever_fn
        self.code_retriever_fn = code_retriever_fn
        self.client = client or get_client()

    async def retrieve(
        self,
        query: str,
        initial_mode: str,
        thinking_profile: Optional[ThinkingProfile] = None,
        satisfaction: Optional[SatisfactionTracker] = None,
        fetch_fn=None,
        code_tool_fn=None,
    ) -> CoordinatedRetrievalResult:
        """
        Execute coordinated retrieval.
        
        Strategy:
          1. Start semantic retrieval (primary)
          2. Monitor decision_llm.needs_code_retriever flag
          3. If True: spawn code_retriever concurrently
          4. Merge both results
          5. Evaluate satisfaction/information gain
          6. Optionally continue with deeper retrieval
        
        Args:
            query: User query
            initial_mode: SEMANTIC | CODE | HYBRID | PARAMETRIC
            thinking_profile: Depth and specificity parameters
            satisfaction: Satisfaction tracker for adaptive depth
            fetch_fn: Optional web fetch function
            code_tool_fn: Optional code tool function
        
        Returns:
            CoordinatedRetrievalResult with merged learnings
        """
        result = CoordinatedRetrievalResult()
        
        # Defaults
        thinking_profile = thinking_profile or ThinkingProfile(
            max_depth=3, budget_s=30.0, use_deep_propositions=False,
            use_critique=False, use_multi_query_expansion=False,
            prompt_specificity="standard", self_consistency_calls=1
        )
        
        # Case 1: PARAMETRIC mode → no retrieval needed
        if initial_mode == "PARAMETRIC":
            result.success = True
            result.message = "Parametric mode: answering from model knowledge"
            return result
        
        # Case 2: CODE mode → code retrieval only
        if initial_mode == "CODE":
            code_result = await self._execute_code_retrieval(
                query, thinking_profile, code_tool_fn
            )
            result.learnings = code_result.learnings
            result.source_urls = code_result.source_urls
            result.used_code_retriever = True
            result.success = code_result.success
            result.error_reason = code_result.error_reason
            result.depth_reached = 1
            result.message = f"Code retrieval: {len(result.learnings)} results"
            return result
        
        # Case 3: SEMANTIC or HYBRID → semantic primary, code adaptive
        sem_result = await self._execute_semantic_retrieval(
            query, thinking_profile, fetch_fn, code_tool_fn, initial_mode
        )
        
        result.learnings = sem_result.get("learnings", [])
        result.source_urls = sem_result.get("source_urls", [])
        result.used_semantic_retriever = True
        result.success = sem_result.get("success", False)
        result.error_reason = sem_result.get("error_reason", "")
        result.depth_reached = sem_result.get("depth", 1)
        
        # Check if code retrieval is needed (this is the KEY integration point!)
        needs_code = sem_result.get("needs_code_retriever", False)
        
        if needs_code or initial_mode == "HYBRID":
            # Spawn code retriever concurrently
            code_result = await self._execute_code_retrieval(
                query, thinking_profile, code_tool_fn
            )
            
            # Merge code results
            result.learnings.extend(code_result.learnings)
            result.source_urls.extend(code_result.source_urls)
            result.used_code_retriever = True
            
            # Deduplicate source URLs
            result.source_urls = list(set(result.source_urls))
            
            message_parts = [f"Semantic retrieval: {len(sem_result.get('learnings', []))} results"]
            if code_result.success:
                message_parts.append(f"Code retrieval: {len(code_result.learnings)} results")
            result.message = ", ".join(message_parts)
        else:
            result.message = f"Semantic retrieval: {len(result.learnings)} results"
        
        # Evaluate satisfaction and consider deeper retrieval
        if satisfaction and thinking_profile.max_depth > result.depth_reached:
            # Simplified: if satisfaction indicates need for more depth, flag it
            if satisfaction.severity_score > 1.0:
                logger.info(
                    "Satisfaction severity %.1f indicates need for deeper retrieval",
                    satisfaction.severity_score
                )
                # Note: actual deeper retrieval would be orchestrated by caller
        
        return result

    async def _execute_semantic_retrieval(
        self,
        query: str,
        thinking_profile: ThinkingProfile,
        fetch_fn,
        code_tool_fn,
        mode: str,
    ) -> dict:
        """Execute semantic retrieval block."""
        try:
            # Build subagent input
            sub_input = SubagentInput(
                task=query,
                subagent_type=SubagentType.RETRIEVER,
                payload={
                    "query": query,
                    "mode": "public",  # or "kb"
                    "fetch_fn": fetch_fn,
                    "code_tool_fn": code_tool_fn,
                    "max_depth": thinking_profile.max_depth,
                },
            )
            
            # Execute semantic retriever
            result = await self.semantic_retriever_fn(sub_input)
            
            # Extract result
            return {
                "success": result.success if hasattr(result, "success") else False,
                "learnings": result.learnings if hasattr(result, "learnings") else [],
                "source_urls": result.source_urls if hasattr(result, "source_urls") else [],
                "error_reason": result.error_reason if hasattr(result, "error_reason") else "",
                "depth": getattr(result, "depth", 1),
                # This is the KEY flag: does semantic retrieval say it needs code?
                "needs_code_retriever": getattr(result, "needs_code_retriever", False),
            }
        except Exception as e:
            logger.error("Semantic retrieval error: %s", e)
            return {
                "success": False,
                "learnings": [],
                "source_urls": [],
                "error_reason": str(e),
                "depth": 1,
                "needs_code_retriever": False,
            }

    async def _execute_code_retrieval(
        self,
        query: str,
        thinking_profile: ThinkingProfile,
        code_tool_fn,
    ) -> dict:
        """Execute code retrieval block."""
        try:
            sub_input = SubagentInput(
                task=query,
                subagent_type=SubagentType.CODE_RETRIEVER,
                payload={
                    "code_tool_fn": code_tool_fn,
                    "max_depth": thinking_profile.max_depth,
                },
            )
            
            result = await self.code_retriever_fn(sub_input)
            
            return {
                "success": result.success if hasattr(result, "success") else False,
                "learnings": result.learnings if hasattr(result, "learnings") else [],
                "source_urls": result.source_urls if hasattr(result, "source_urls") else [],
                "error_reason": result.error_reason if hasattr(result, "error_reason") else "",
            }
        except Exception as e:
            logger.error("Code retrieval error: %s", e)
            return {
                "success": False,
                "learnings": [],
                "source_urls": [],
                "error_reason": str(e),
            }


async def retrieve_adaptive(
    query: str,
    initial_mode: str,
    semantic_retriever_fn: Callable[[SubagentInput], Awaitable],
    code_retriever_fn: Callable[[SubagentInput], Awaitable],
    thinking_profile: Optional[ThinkingProfile] = None,
    satisfaction: Optional[SatisfactionTracker] = None,
    fetch_fn=None,
    code_tool_fn=None,
    client: Optional[NIMClient] = None,
) -> CoordinatedRetrievalResult:
    """
    Convenience function for adaptive retrieval without coordinator instance.
    
    Args:
        query: User query
        initial_mode: SEMANTIC | CODE | HYBRID | PARAMETRIC
        semantic_retriever_fn: Semantic retrieval function
        code_retriever_fn: Code retrieval function
        thinking_profile: Optional depth/specificity parameters
        satisfaction: Optional satisfaction tracker
        fetch_fn: Optional web fetch function
        code_tool_fn: Optional code tool function
        client: Optional LLM client
    
    Returns:
        CoordinatedRetrievalResult
    """
    coordinator = RetrieverCoordinator(
        semantic_retriever_fn=semantic_retriever_fn,
        code_retriever_fn=code_retriever_fn,
        client=client,
    )
    
    return await coordinator.retrieve(
        query=query,
        initial_mode=initial_mode,
        thinking_profile=thinking_profile,
        satisfaction=satisfaction,
        fetch_fn=fetch_fn,
        code_tool_fn=code_tool_fn,
    )
