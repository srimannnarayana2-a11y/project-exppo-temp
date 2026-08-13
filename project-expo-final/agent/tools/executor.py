"""
Parallel Tool Executor — Execute up to 4 tools concurrently

From Jarvis's `executeJarvisToolsParallel()` pattern.

Allows the Agent to run multiple tools in parallel when safe:
  - Multiple search queries
  - File reads + web fetches
  - Code review + tests in parallel
  - Builder tools + file operations

Expected improvement:
  - 4 sequential tool calls: 4× latency
  - 4 parallel tool calls: 1–2× latency (I/O bound)
  - Multi-tool requests: 40–60% latency reduction

Usage:
    from agent.tools.executor import execute_tools_parallel
    
    results = await execute_tools_parallel([
        {"name": "read_file", "args": {"file_path": "/path/to/file"}},
        {"name": "semantic_search", "args": {"query": "something"}},
        {"name": "code_search", "args": {"query": "pattern"}},
    ])
    
    for result in results:
        print(result.success, result.output)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional, Callable, Awaitable, List, Any

logger = logging.getLogger(__name__)

# Maximum concurrent tool calls (from Jarvis)
MAX_PARALLEL = 4


# ─── Result Types ──────────────────────────────────────────────────────────────

@dataclass
class ToolCallResult:
    """Result from executing one tool call."""
    tool_name: str
    success: bool
    output: str = ""
    error: str = ""
    duration_ms: float = 0.0
    
    def __str__(self) -> str:
        if self.success:
            return f"✓ {self.tool_name}: {self.output[:100]}"
        else:
            return f"✗ {self.tool_name}: {self.error}"


# ─── Tool Call Request ─────────────────────────────────────────────────────────

@dataclass
class ToolCall:
    """One tool call request."""
    tool_name: str
    args: dict[str, Any]
    
    def __str__(self) -> str:
        return f"{self.tool_name}({', '.join(f'{k}={v!r}' for k, v in self.args.items())})"


# ─── Tool Executor Interface ───────────────────────────────────────────────────

class ToolExecutor:
    """
    Executes tools with parallelization support.
    
    Subclasses should implement execute_tool() to dispatch to actual tool implementations.
    """
    
    async def execute_tool(self, tool_name: str, args: dict[str, Any]) -> ToolCallResult:
        """
        Execute a single tool call.
        
        Override in subclass to provide actual tool implementations.
        
        Args:
            tool_name: Name of tool to execute
            args: Tool arguments
        
        Returns:
            ToolCallResult with success/output/error/duration
        """
        raise NotImplementedError("Subclass must implement execute_tool()")
    
    async def execute_parallel(
        self,
        calls: List[dict[str, Any]],
        max_concurrent: int = MAX_PARALLEL,
    ) -> List[ToolCallResult]:
        """
        Execute multiple tool calls in parallel (with concurrency limit).
        
        Args:
            calls: List of {"name": str, "args": dict} tool call specs
            max_concurrent: Max concurrent executions (default 4)
        
        Returns:
            List of ToolCallResult in same order as input calls
        """
        if not calls:
            return []
        
        # Semaphore limits concurrency to max_concurrent
        semaphore = asyncio.Semaphore(max_concurrent)
        
        async def run_with_limit(call: dict[str, Any]) -> ToolCallResult:
            async with semaphore:
                tool_name = call.get("name", "unknown")
                args = call.get("args", {})
                
                try:
                    result = await self.execute_tool(tool_name, args)
                    logger.debug(f"Tool {tool_name} completed: success={result.success}")
                    return result
                except Exception as e:
                    logger.error(f"Tool {tool_name} failed with exception: {e}")
                    return ToolCallResult(
                        tool_name=tool_name,
                        success=False,
                        error=str(e),
                    )
        
        # Run all calls concurrently
        tasks = [run_with_limit(call) for call in calls]
        results = await asyncio.gather(*tasks)
        
        # Summary
        successful = sum(1 for r in results if r.success)
        total_ms = sum(r.duration_ms for r in results)
        logger.info(
            f"Executed {len(results)} tools in parallel: "
            f"{successful}/{len(results)} successful, "
            f"{total_ms:.0f}ms total"
        )
        
        return results


# ─── Default Executor (Stub) ───────────────────────────────────────────────────

class DefaultToolExecutor(ToolExecutor):
    """
    Default executor that dispatches to registered handlers.
    
    Can be extended with custom tool handlers.
    """
    
    def __init__(self, handlers: Optional[dict[str, Callable]] = None):
        """
        Initialize executor.
        
        Args:
            handlers: Optional dict mapping tool_name -> async handler function
        """
        self.handlers = handlers or {}
    
    def register_handler(
        self,
        tool_name: str,
        handler: Callable[[dict[str, Any]], Awaitable[ToolCallResult]]
    ) -> None:
        """Register a handler for a tool."""
        self.handlers[tool_name] = handler
    
    async def execute_tool(self, tool_name: str, args: dict[str, Any]) -> ToolCallResult:
        """
        Execute a tool using registered handler or return error.
        """
        import time
        start = time.time()
        
        handler = self.handlers.get(tool_name)
        if not handler:
            return ToolCallResult(
                tool_name=tool_name,
                success=False,
                error=f"No handler registered for tool: {tool_name}",
                duration_ms=(time.time() - start) * 1000,
            )
        
        try:
            result = await handler(args)
            result.duration_ms = (time.time() - start) * 1000
            return result
        except Exception as e:
            return ToolCallResult(
                tool_name=tool_name,
                success=False,
                error=str(e),
                duration_ms=(time.time() - start) * 1000,
            )


# ─── Utility Functions ─────────────────────────────────────────────────────────

async def execute_tools_parallel(
    calls: List[dict[str, Any]],
    executor: Optional[ToolExecutor] = None,
    max_concurrent: int = MAX_PARALLEL,
) -> List[ToolCallResult]:
    """
    Execute multiple tools in parallel.
    
    Convenience function using global executor.
    
    Args:
        calls: List of {"name": str, "args": dict} tool call specs
        executor: Custom executor (uses default if None)
        max_concurrent: Max concurrent executions
    
    Returns:
        List of ToolCallResult in same order as input calls
    """
    if executor is None:
        executor = _get_default_executor()
    
    return await executor.execute_parallel(calls, max_concurrent)


# ─── Global Default Executor ──────────────────────────────────────────────────

_DEFAULT_EXECUTOR: Optional[DefaultToolExecutor] = None


def get_default_executor() -> DefaultToolExecutor:
    """Get or create global default executor."""
    global _DEFAULT_EXECUTOR
    if _DEFAULT_EXECUTOR is None:
        _DEFAULT_EXECUTOR = DefaultToolExecutor()
        logger.info("Initialized DefaultToolExecutor")
    return _DEFAULT_EXECUTOR


def _get_default_executor() -> DefaultToolExecutor:
    """Internal alias for get_default_executor()."""
    return get_default_executor()


def register_tool_handler(
    tool_name: str,
    handler: Callable[[dict[str, Any]], Awaitable[ToolCallResult]]
) -> None:
    """Register a tool handler globally."""
    executor = get_default_executor()
    executor.register_handler(tool_name, handler)
