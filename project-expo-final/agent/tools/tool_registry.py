"""
Tool Registry — Comprehensive 26-tool system ported from Jarvis

Tools organized by category:
  - File Tools (5): read, write, edit, list, bash
  - Search Tools (4): google, bing, serpapi, mcp
  - RAG Tools (3): semantic_search, code_search, kb_load
  - Agent Tools (4): think_long, delegate, clarify, stream
  - Builder Tools (4): build_deck, build_report, build_sheet, build_dashboard
  - Code Tools (3): review_code, explain_code, fix_bug
  - Other (3): todo_add, todo_list, todo_complete

All tools have consistent schema:
  - name: str (unique tool identifier)
  - description: str (what it does)
  - parameters: dict (parameter definitions)
  - required: list[str] (required parameters)

This module:
1. Defines all tool schemas
2. Provides registry lookup (by name, by category, by intent)
3. Enables tool executor to dispatch to handlers
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional, Callable, Any, Dict, List
from enum import Enum

logger = logging.getLogger(__name__)


# ─── Tool Categories ──────────────────────────────────────────────────────────

class ToolCategory(str, Enum):
    FILE = "file"
    SEARCH = "search"
    RAG = "rag"
    AGENT = "agent"
    BUILDER = "builder"
    CODE = "code"
    TODO = "todo"


# ─── Tool Schema ───────────────────────────────────────────────────────────────

@dataclass
class ToolParameter:
    """One parameter in a tool's parameter schema."""
    name: str
    type: str  # "string" | "number" | "boolean" | "array" | "object"
    description: str
    required: bool = False
    default: Any = None
    enum: Optional[List[str]] = None  # For constrained values


@dataclass
class ToolDefinition:
    """Complete tool definition — API contract."""
    name: str
    description: str
    category: ToolCategory
    parameters: List[ToolParameter]
    
    def to_openai_function_schema(self) -> dict:
        """Convert to OpenAI function calling schema."""
        props = {}
        required = []
        
        for param in self.parameters:
            param_schema: dict[str, Any] = {
                "type": param.type,
                "description": param.description,
            }
            if param.enum:
                param_schema["enum"] = param.enum
            if param.default is not None:
                param_schema["default"] = param.default
            
            props[param.name] = param_schema
            if param.required:
                required.append(param.name)
        
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": props,
                    "required": required,
                }
            }
        }


# ─── Tool Registry ────────────────────────────────────────────────────────────

class ToolRegistry:
    """Centralized tool registry — lookup by name, category, or intent."""
    
    def __init__(self):
        self.tools: Dict[str, ToolDefinition] = {}
        self._register_all_tools()
    
    def register(self, tool: ToolDefinition) -> None:
        """Register a tool."""
        if tool.name in self.tools:
            logger.warning(f"Overwriting tool: {tool.name}")
        self.tools[tool.name] = tool
    
    def get(self, name: str) -> Optional[ToolDefinition]:
        """Get tool by name."""
        return self.tools.get(name)
    
    def by_category(self, category: ToolCategory) -> List[ToolDefinition]:
        """Get all tools in a category."""
        return [t for t in self.tools.values() if t.category == category]
    
    def list_all(self) -> List[ToolDefinition]:
        """Get all tools."""
        return list(self.tools.values())
    
    def openai_schemas(self) -> List[dict]:
        """Get all tools as OpenAI function schemas."""
        return [t.to_openai_function_schema() for t in self.tools.values()]
    
    def by_intent(self, intent: str) -> List[ToolDefinition]:
        """
        Get tool subset for a specific intent class.
        
        Intent classes (from Jarvis):
          - build_document: Builder tools + file tools
          - code_task: Code tools + file tools
          - data_task: Builder (sheet) + rag tools
          - research: Search + RAG tools
          - file_op: File tools + search
          - system_cmd: File tools (bash)
          - query: All tools
        """
        intent_lower = intent.lower()
        
        if "document" in intent_lower or "report" in intent_lower or "deck" in intent_lower:
            return self.by_category(ToolCategory.BUILDER) + self.by_category(ToolCategory.FILE)
        elif "code" in intent_lower or "review" in intent_lower or "explain" in intent_lower:
            return self.by_category(ToolCategory.CODE) + self.by_category(ToolCategory.FILE)
        elif "data" in intent_lower or "sheet" in intent_lower or "dashboard" in intent_lower:
            return (self.by_category(ToolCategory.BUILDER) + 
                   self.by_category(ToolCategory.RAG) + 
                   self.by_category(ToolCategory.FILE))
        elif "research" in intent_lower or "search" in intent_lower:
            return self.by_category(ToolCategory.SEARCH) + self.by_category(ToolCategory.RAG)
        elif "file" in intent_lower:
            return self.by_category(ToolCategory.FILE) + self.by_category(ToolCategory.SEARCH)
        elif "system" in intent_lower or "bash" in intent_lower or "git" in intent_lower:
            return self.by_category(ToolCategory.FILE)
        else:
            # Default: all tools
            return self.list_all()
    
    def _register_all_tools(self) -> None:
        """Register all 26 tools."""
        
        # ─── FILE TOOLS (5) ───────────────────────────────────────────────────
        
        self.register(ToolDefinition(
            name="read_file",
            description="Read a file or file range. Cached for full reads.",
            category=ToolCategory.FILE,
            parameters=[
                ToolParameter("file_path", "string", "Absolute path to file", required=True),
                ToolParameter("start_line", "number", "Start line (1-indexed)", default=1),
                ToolParameter("end_line", "number", "End line (1-indexed)"),
            ]
        ))
        
        self.register(ToolDefinition(
            name="write_file",
            description="Write or overwrite a file. Invalidates cache.",
            category=ToolCategory.FILE,
            parameters=[
                ToolParameter("file_path", "string", "Absolute path to file", required=True),
                ToolParameter("content", "string", "File content to write", required=True),
            ]
        ))
        
        self.register(ToolDefinition(
            name="edit_file",
            description="Replace text in a file (read-patch-write). Cache-aware.",
            category=ToolCategory.FILE,
            parameters=[
                ToolParameter("file_path", "string", "Absolute path to file", required=True),
                ToolParameter("old_string", "string", "Text to replace (must be exact match)", required=True),
                ToolParameter("new_string", "string", "Replacement text", required=True),
                ToolParameter("replace_all", "boolean", "Replace all occurrences", default=False),
            ]
        ))
        
        self.register(ToolDefinition(
            name="list_directory",
            description="List directory contents with sizes and file type indicators.",
            category=ToolCategory.FILE,
            parameters=[
                ToolParameter("path", "string", "Directory path", required=True),
                ToolParameter("recurse", "boolean", "Recursively list subdirs", default=False),
            ]
        ))
        
        self.register(ToolDefinition(
            name="bash",
            description="Execute bash command in sandbox. Returns stdout/stderr.",
            category=ToolCategory.FILE,
            parameters=[
                ToolParameter("command", "string", "Bash command to run", required=True),
                ToolParameter("timeout", "number", "Timeout in seconds", default=30),
                ToolParameter("cwd", "string", "Working directory (defaults to /tmp/agent_sandbox)"),
            ]
        ))
        
        # ─── SEARCH TOOLS (4) ──────────────────────────────────────────────────
        
        self.register(ToolDefinition(
            name="google_search",
            description="Search Google. Returns top results with snippets.",
            category=ToolCategory.SEARCH,
            parameters=[
                ToolParameter("query", "string", "Search query", required=True),
                ToolParameter("num_results", "number", "Number of results (1-20)", default=8),
            ]
        ))
        
        self.register(ToolDefinition(
            name="bing_search",
            description="Search Bing. Returns top results with snippets.",
            category=ToolCategory.SEARCH,
            parameters=[
                ToolParameter("query", "string", "Search query", required=True),
                ToolParameter("num_results", "number", "Number of results (1-20)", default=8),
            ]
        ))
        
        self.register(ToolDefinition(
            name="serpapi_search",
            description="Search via SerpAPI (Google, Bing, Baidu). Returns rich results.",
            category=ToolCategory.SEARCH,
            parameters=[
                ToolParameter("query", "string", "Search query", required=True),
                ToolParameter("engine", "string", "Search engine", 
                            enum=["google", "bing", "baidu"], default="google"),
                ToolParameter("num_results", "number", "Number of results (1-20)", default=10),
            ]
        ))
        
        self.register(ToolDefinition(
            name="mcp_search",
            description="Search via Model Context Protocol (MCP) integration.",
            category=ToolCategory.SEARCH,
            parameters=[
                ToolParameter("query", "string", "Search query", required=True),
                ToolParameter("provider", "string", "MCP provider", default="default"),
            ]
        ))
        
        # ─── RAG TOOLS (3) ────────────────────────────────────────────────────
        
        self.register(ToolDefinition(
            name="semantic_search",
            description="Search knowledge base via semantic embeddings.",
            category=ToolCategory.RAG,
            parameters=[
                ToolParameter("query", "string", "Search query", required=True),
                ToolParameter("top_k", "number", "Number of results", default=5),
                ToolParameter("threshold", "number", "Similarity threshold (0-1)", default=0.5),
            ]
        ))
        
        self.register(ToolDefinition(
            name="code_search",
            description="Search code repositories (GitHub, local). Returns code snippets.",
            category=ToolCategory.RAG,
            parameters=[
                ToolParameter("query", "string", "Search query", required=True),
                ToolParameter("language", "string", "Programming language filter"),
                ToolParameter("num_results", "number", "Number of results", default=5),
            ]
        ))
        
        self.register(ToolDefinition(
            name="kb_load",
            description="Load knowledge base document by ID or path.",
            category=ToolCategory.RAG,
            parameters=[
                ToolParameter("doc_id", "string", "Document ID or path", required=True),
                ToolParameter("format", "string", "Format (text/markdown/html)", default="text"),
            ]
        ))
        
        # ─── AGENT TOOLS (4) ───────────────────────────────────────────────────
        
        self.register(ToolDefinition(
            name="think_long",
            description="Request extended thinking/reasoning from the LLM.",
            category=ToolCategory.AGENT,
            parameters=[
                ToolParameter("prompt", "string", "Thinking prompt", required=True),
                ToolParameter("max_tokens", "number", "Max thinking tokens", default=10000),
            ]
        ))
        
        self.register(ToolDefinition(
            name="delegate_task",
            description="Delegate a subtask to a specialized subagent.",
            category=ToolCategory.AGENT,
            parameters=[
                ToolParameter("task", "string", "Task description", required=True),
                ToolParameter("subagent_type", "string", 
                            enum=["retriever", "code_retriever", "sandbox"],
                            required=True),
                ToolParameter("payload", "object", "Task payload"),
            ]
        ))
        
        self.register(ToolDefinition(
            name="request_clarification",
            description="Ask the user a clarifying question.",
            category=ToolCategory.AGENT,
            parameters=[
                ToolParameter("question", "string", "Clarifying question", required=True),
                ToolParameter("context", "string", "Why you're asking"),
            ]
        ))
        
        self.register(ToolDefinition(
            name="stream_output",
            description="Stream output incrementally (for long-running tasks).",
            category=ToolCategory.AGENT,
            parameters=[
                ToolParameter("content", "string", "Content to stream", required=True),
                ToolParameter("stream_type", "string", 
                            enum=["thinking", "working", "result"], default="working"),
            ]
        ))
        
        # ─── BUILDER TOOLS (4) ─────────────────────────────────────────────────
        
        self.register(ToolDefinition(
            name="build_deck",
            description="Build a PowerPoint presentation (PPTX) from spec.",
            category=ToolCategory.BUILDER,
            parameters=[
                ToolParameter("spec", "object", "Deck specification (theme, slides, etc.)", required=True),
                ToolParameter("output_path", "string", "Output file path", required=True),
                ToolParameter("auto_open", "boolean", "Open file after creation", default=False),
            ]
        ))
        
        self.register(ToolDefinition(
            name="build_report",
            description="Build a DOCX/PDF report from spec.",
            category=ToolCategory.BUILDER,
            parameters=[
                ToolParameter("spec", "object", "Report specification", required=True),
                ToolParameter("output_path", "string", "Output file path", required=True),
                ToolParameter("formats", "array", "Output formats: [docx] or [pdf] or [docx, pdf]", 
                            default=["docx"]),
            ]
        ))
        
        self.register(ToolDefinition(
            name="build_sheet",
            description="Build an Excel spreadsheet from spec.",
            category=ToolCategory.BUILDER,
            parameters=[
                ToolParameter("spec", "object", "Sheet specification (title, sheets, data)", required=True),
                ToolParameter("output_path", "string", "Output file path", required=True),
            ]
        ))
        
        self.register(ToolDefinition(
            name="build_dashboard",
            description="Build an interactive HTML/React dashboard from spec.",
            category=ToolCategory.BUILDER,
            parameters=[
                ToolParameter("spec", "object", "Dashboard specification (theme, sections)", required=True),
                ToolParameter("output_path", "string", "Output file path", required=True),
                ToolParameter("port", "number", "Port for preview server", default=3000),
            ]
        ))
        
        # ─── CODE TOOLS (3) ────────────────────────────────────────────────────
        
        self.register(ToolDefinition(
            name="review_code",
            description="Review code for bugs, style, and best practices.",
            category=ToolCategory.CODE,
            parameters=[
                ToolParameter("code", "string", "Code to review", required=True),
                ToolParameter("language", "string", "Programming language", required=True),
                ToolParameter("focus", "string", "Review focus (bugs/style/perf/security)", 
                            enum=["bugs", "style", "perf", "security", "all"], default="all"),
            ]
        ))
        
        self.register(ToolDefinition(
            name="explain_code",
            description="Explain what code does in plain English.",
            category=ToolCategory.CODE,
            parameters=[
                ToolParameter("code", "string", "Code to explain", required=True),
                ToolParameter("language", "string", "Programming language", required=True),
                ToolParameter("depth", "string", "Explanation depth", 
                            enum=["brief", "medium", "detailed"], default="medium"),
            ]
        ))
        
        self.register(ToolDefinition(
            name="fix_bug",
            description="Fix a bug in code. Returns corrected code.",
            category=ToolCategory.CODE,
            parameters=[
                ToolParameter("code", "string", "Buggy code", required=True),
                ToolParameter("language", "string", "Programming language", required=True),
                ToolParameter("bug_description", "string", "What's broken", required=True),
            ]
        ))
        
        # ─── TODO TOOLS (3) ────────────────────────────────────────────────────
        
        self.register(ToolDefinition(
            name="todo_add",
            description="Add an item to the todo list.",
            category=ToolCategory.TODO,
            parameters=[
                ToolParameter("title", "string", "Todo item title", required=True),
                ToolParameter("priority", "string", "Priority (low/medium/high)", default="medium"),
                ToolParameter("due_date", "string", "Due date (YYYY-MM-DD)"),
            ]
        ))
        
        self.register(ToolDefinition(
            name="todo_list",
            description="List all todo items. Filter by status (open/completed).",
            category=ToolCategory.TODO,
            parameters=[
                ToolParameter("status", "string", "Filter by status", 
                            enum=["open", "completed", "all"], default="open"),
            ]
        ))
        
        self.register(ToolDefinition(
            name="todo_complete",
            description="Mark a todo item as complete.",
            category=ToolCategory.TODO,
            parameters=[
                ToolParameter("todo_id", "string", "Todo item ID", required=True),
            ]
        ))


# ─── Global Registry ──────────────────────────────────────────────────────────

_REGISTRY: Optional[ToolRegistry] = None


def get_tool_registry() -> ToolRegistry:
    """Get or create the global tool registry."""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = ToolRegistry()
        logger.info(f"Initialized ToolRegistry with {len(_REGISTRY.tools)} tools")
    return _REGISTRY


def get_tools_for_intent(intent: str) -> List[ToolDefinition]:
    """Get tool subset for a specific intent."""
    registry = get_tool_registry()
    return registry.by_intent(intent)


def get_tool_openai_schemas(intent: Optional[str] = None) -> List[dict]:
    """
    Get tool schemas for OpenAI function calling.
    
    Args:
        intent: If provided, return only tools for that intent
    
    Returns:
        List of OpenAI function schemas
    """
    registry = get_tool_registry()
    if intent:
        tools = get_tools_for_intent(intent)
    else:
        tools = registry.list_all()
    return [t.to_openai_function_schema() for t in tools]
