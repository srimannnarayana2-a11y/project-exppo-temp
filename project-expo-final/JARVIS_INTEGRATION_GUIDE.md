"""
JARVIS ENHANCEMENTS INTEGRATION GUIDE

These new modules add Jarvis capabilities to Agent without breaking existing code.
All enhancements are OPTIONAL and can be adopted incrementally.

────────────────────────────────────────────────────────────────────────────────
 PHASE 1: Foundation Modules (Already Implemented)
────────────────────────────────────────────────────────────────────────────────

1. LRU Cache Layer (agent/cache/lru_cache.py)
   ├─ FileCache: File read caching with mtime invalidation
   ├─ LRUCache: Generic LRU cache with TTL support
   └─ Global caches: file_cache (100 cap), query_cache (256 cap)
   
   USAGE:
       from agent.cache.lru_cache import get_file_cache, get_query_cache
       
       file_cache = get_file_cache()
       content = file_cache.get_or_read("/path/to/file")
       
       query_cache = get_query_cache()
       result = query_cache.get_or_fetch("query_key", lambda: expensive_operation())

2. Tool Registry (agent/tools/tool_registry.py)
   ├─ 26 tools across 7 categories
   ├─ Intent-aware tool filtering (build_document, code_task, data_task, etc.)
   ├─ OpenAI function calling schema export
   └─ Category-based lookup
   
   USAGE:
       from agent.tools.tool_registry import get_tool_registry, get_tools_for_intent
       
       registry = get_tool_registry()
       all_tools = registry.list_all()
       
       # Get tool subset for specific intent
       tools = get_tools_for_intent("build_document")
       
       # Export for OpenAI function calling
       schemas = registry.openai_schemas()

3. Parallel Tool Executor (agent/tools/executor.py)
   ├─ Execute up to 4 tools concurrently
   ├─ Individual tool timeouts
   ├─ Fault tolerance (allSettled-style)
   └─ Custom handler registration
   
   USAGE:
       from agent.tools.executor import execute_tools_parallel
       
       results = await execute_tools_parallel([
           {"name": "read_file", "args": {"file_path": "/src/main.py"}},
           {"name": "semantic_search", "args": {"query": "database"}},
           {"name": "code_search", "args": {"query": "connection pool"}},
       ])
       
       for result in results:
           print(f"{result.tool_name}: {result.output}")

4. Context Policy (agent/core/context_policy.py)
   ├─ Token estimation (4 chars ≈ 1 token)
   ├─ Context status tracking (% full, warnings)
   ├─ Auto-summarization (50K token threshold)
   ├─ Session-aware context prep
   └─ Token budget hints for LLM
   
   USAGE:
       from agent.core.context_policy import (
           get_context_status, auto_summarize_history,
           prepare_history_for_api_call, build_token_budget_hint
       )
       
       # Check context health
       status = get_context_status(history)
       if status.should_summarize:
           history, summarized, kept = auto_summarize_history(history)
       
       # Prepare for API call with auto-summarization
       history = prepare_history_for_api_call(history)
       
       # Get hint for LLM about token budget
       hint = build_token_budget_hint(status)

5. Session Store (agent/memory/session_store.py)
   ├─ Persistent conversation history (.agent/session.json)
   ├─ Session metadata (created_at, message_count, etc.)
   ├─ Multi-session support
   ├─ Atomic save/load
   └─ Validation
   
   USAGE:
       from agent.memory.session_store import (
           SessionStore, save_session, load_session,
           get_session_store, session_exists
       )
       
       # Use global store
       history = load_session()
       if not history:
           history = [{"role": "system", "content": "You are an agent."}]
       
       # ... do work ...
       
       save_session(history)
       
       # Or use store directly
       store = get_session_store()
       meta = store.get_metadata()
       print(f"Session {meta.session_id}: {meta.message_count} messages")

6. Intent Classifier (agent/routing/intent_classifier.py)
   ├─ 7 intent classes
   ├─ Regex pattern matching
   ├─ Confidence scoring
   ├─ Tool subset selection
   └─ System prompt injection
   
   USAGE:
       from agent.routing.intent_classifier import classify_intent
       
       result = classify_intent("build a quarterly report")
       print(result.intent)              # "build_document"
       print(result.confidence)          # 0.95
       print(result.primary_tools)       # ["build_report", "read_file", ...]
       print(result.should_plan_first)   # True
       print(result.system_addendum)     # Specific guidance

────────────────────────────────────────────────────────────────────────────────
 PHASE 2: Integration Patterns (Recommended)
────────────────────────────────────────────────────────────────────────────────

Pattern 1: Use LRU Cache in File Operations
────────────────────────────────────────────
BEFORE (no caching):
    def read_file(path: str) -> str:
        return open(path).read()

AFTER (with LRU cache):
    from agent.cache.lru_cache import get_file_cache
    
    def read_file(path: str) -> str:
        cache = get_file_cache()
        return cache.get_or_read(path)

Pattern 2: Use Intent Classification in query.py
───────────────────────────────────────────────
BEFORE (all tools available):
    async def run_query(query: str) -> QueryResult:
        # ... entry_gate, orchestrator, synthesis ...

AFTER (intent-aware tools):
    from agent.routing.intent_classifier import classify_intent
    
    async def run_query(query: str) -> QueryResult:
        intent_result = classify_intent(query)
        
        # Narrow tool set based on intent
        tools = get_tools_for_intent(intent_result.intent)
        
        # Add system addendum
        system_prompt += "\n\n" + intent_result.system_addendum
        
        # Proceed with orchestrator
        # ... entry_gate, orchestrator, synthesis ...

Pattern 3: Use Auto-Summarization in query.py
──────────────────────────────────────────────
BEFORE (no auto-summarization):
    async def run_query(query: str, history: List[Dict]) -> QueryResult:
        # ... use history directly ...

AFTER (with context management):
    from agent.core.context_policy import (
        prepare_history_for_api_call,
        get_context_status,
        build_token_budget_hint
    )
    
    async def run_query(query: str, history: List[Dict]) -> QueryResult:
        # Auto-summarize if needed
        history = prepare_history_for_api_call(history)
        
        # Add token budget hint to system prompt
        status = get_context_status(history)
        if status.warning:
            system_prompt += f"\n\n{build_token_budget_hint(status)}"
        
        # ... rest of query pipeline ...

Pattern 4: Use Session Persistence
──────────────────────────────────
BEFORE (queries are isolated):
    async def run_query(query: str) -> QueryResult:
        # ... no session loading ...

AFTER (resume on restart):
    from agent.memory.session_store import load_session, save_session
    
    async def main():
        # Load previous session if exists
        history = load_session()
        if not history:
            history = [{"role": "system", "content": "..."}]
        
        while True:
            query = input("> ")
            result = await run_query(query, history)
            
            # Build history as you go
            history.append({"role": "user", "content": query})
            history.append({"role": "assistant", "content": result.answer})
            
            # Save session
            save_session(history)

Pattern 5: Use Parallel Tool Execution in Orchestrator
──────────────────────────────────────────────────────
BEFORE (sequential tool execution):
    result1 = await execute_tool("read_file", args)
    result2 = await execute_tool("semantic_search", args)
    result3 = await execute_tool("code_search", args)

AFTER (parallel execution):
    from agent.tools.executor import execute_tools_parallel
    
    results = await execute_tools_parallel([
        {"name": "read_file", "args": {...}},
        {"name": "semantic_search", "args": {...}},
        {"name": "code_search", "args": {...}},
    ])
    
    # 3 tools: was 3× latency, now ~1.5× latency (I/O bound)

Pattern 6: Opt-in Tool Narrowing in Orchestrator
────────────────────────────────────────────────
BEFORE (orchestrator sees all 26 tools):
    async def run_orchestrator(task: str) -> SubagentResult:
        tools = get_all_tools()  # 26 tools
        # ... LLM sees bloat ...

AFTER (orchestrator sees only relevant tools):
    from agent.routing.intent_classifier import classify_intent
    
    async def run_orchestrator(task: str) -> SubagentResult:
        intent_result = classify_intent(task)
        tools = get_tools_for_intent(intent_result.intent)  # 3–8 tools
        # ... LLM sees focused set ...

────────────────────────────────────────────────────────────────────────────────
 PHASE 3: Advanced Patterns (Coming Later)
────────────────────────────────────────────────────────────────────────────────

- Builder skill executors (Node.js subprocess wrappers)
- Tool handler registration system
- Adaptive RAG integration (already similar to Agent's blocks)
- Multi-key provider optimization for Jarvis tools
- Skill registry enhancement with Jarvis skills

────────────────────────────────────────────────────────────────────────────────
 COMPATIBILITY GUARANTEES
────────────────────────────────────────────────────────────────────────────────

✅ No modifications to existing Agent files
✅ No breaking changes to entry_gate, orchestrator, or query pipeline
✅ All new modules are OPTIONAL (can be disabled)
✅ Existing tests continue to pass (add new tests for new features)
✅ Existing caching (semantic_cache, llm_cache) unaffected

────────────────────────────────────────────────────────────────────────────────
 QUICK START: Adopt One Feature at a Time
────────────────────────────────────────────────────────────────────────────────

Week 1: Add LRU cache to file operations
Week 2: Integrate session persistence
Week 3: Add intent classification
Week 4: Use context policy for token budgeting
Week 5: Enable parallel tool execution

────────────────────────────────────────────────────────────────────────────────
 API REFERENCE
────────────────────────────────────────────────────────────────────────────────

LRU Cache:
  - get_file_cache() -> FileCache
  - get_query_cache() -> LRUCache[str]
  - cache.get(key) -> Optional[T]
  - cache.set(key, value, ttl_seconds=0) -> None
  - cache.get_or_fetch(key, fetch_fn, ttl_seconds=0) -> T
  - cache_stats() -> dict

Tool Registry:
  - get_tool_registry() -> ToolRegistry
  - get_tools_for_intent(intent: str) -> List[ToolDefinition]
  - get_tool_openai_schemas(intent=None) -> List[dict]
  - registry.get(name) -> Optional[ToolDefinition]
  - registry.by_category(category) -> List[ToolDefinition]
  - registry.list_all() -> List[ToolDefinition]

Executor:
  - execute_tools_parallel(calls, executor=None, max_concurrent=4) -> List[ToolCallResult]
  - get_default_executor() -> DefaultToolExecutor
  - register_tool_handler(tool_name, async_handler_fn) -> None

Context Policy:
  - estimate_tokens(history) -> int
  - get_context_status(history) -> ContextStatus
  - auto_summarize_history(history, keep_recent=6) -> (history, was_summarized, kept_count)
  - prepare_history_for_api_call(history, auto_summarize=True) -> history
  - build_token_budget_hint(status) -> str
  - validate_history(history) -> (is_valid, error_message)
  - visualize_history(history, max_messages=20) -> str

Session Store:
  - get_session_store(session_dir=None, cwd=None) -> SessionStore
  - save_session(history) -> None
  - load_session() -> Optional[List[Dict]]
  - clear_session() -> None
  - session_exists() -> bool
  - get_session_metadata() -> Optional[SessionMetadata]

Intent Classifier:
  - classify_intent(query: str) -> IntentClassification
  - IntentClassification attributes:
    - intent: str (one of 7 intent classes)
    - confidence: float (0–1)
    - rationale: str
    - primary_tools: list[str]
    - system_addendum: str
    - should_plan_first: bool
    - should_verify: bool
    - parallelizable: bool

────────────────────────────────────────────────────────────────────────────────
 TESTING THE ENHANCEMENTS
────────────────────────────────────────────────────────────────────────────────

1. Test LRU cache:
   python -c "from agent.cache.lru_cache import get_file_cache; c = get_file_cache(); print(c.stats())"

2. Test tool registry:
   python -c "from agent.tools.tool_registry import get_tool_registry; r = get_tool_registry(); print(len(r.tools), 'tools')"

3. Test intent classification:
   python -c "from agent.routing.intent_classifier import classify_intent; print(classify_intent('make a presentation').intent)"

4. Test context policy:
   python -c "from agent.core.context_policy import estimate_tokens; print(estimate_tokens([{'role': 'user', 'content': 'hello'}]), 'tokens')"

5. Test session store:
   python -c "from agent.memory.session_store import session_exists; print('Session exists:', session_exists())"

6. Full test suite:
   pytest -v tests/

────────────────────────────────────────────────────────────────────────────────
 QUESTIONS?
────────────────────────────────────────────────────────────────────────────────

Refer to individual module docstrings:
  - agent/cache/lru_cache.py
  - agent/tools/tool_registry.py
  - agent/tools/executor.py
  - agent/core/context_policy.py
  - agent/memory/session_store.py
  - agent/routing/intent_classifier.py
"""

# This file is documentation. Run any examples above to test integration.
# To use these features, import from the modules listed above.

if __name__ == "__main__":
    print(__doc__)
