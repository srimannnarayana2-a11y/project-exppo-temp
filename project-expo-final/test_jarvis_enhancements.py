#!/usr/bin/env python3
"""
Quick validation test for all Jarvis enhancement modules.

Run: python test_jarvis_enhancements.py
"""

import asyncio
import tempfile
import sys
from pathlib import Path


def test_lru_cache():
    """Test LRU cache functionality."""
    print("\n" + "="*60)
    print("Testing LRU Cache...")
    print("="*60)
    
    from agent.cache.lru_cache import LRUCache, get_file_cache, cache_stats
    
    # Test basic LRU
    cache = LRUCache[str](capacity=3)
    cache.set("a", "value_a")
    cache.set("b", "value_b")
    cache.set("c", "value_c")
    
    assert cache.get("a") == "value_a", "Basic get failed"
    assert cache.get("b") == "value_b", "Basic get failed"
    
    # Test eviction
    cache.set("d", "value_d")  # Should evict least-recently-used
    assert cache.get("c") == "value_c", "Get after eviction failed"
    
    # Test TTL
    cache.set("ttl_key", "ttl_value", ttl_seconds=0.01)
    assert cache.get("ttl_key") == "ttl_value", "TTL get before expiry failed"
    
    import time
    time.sleep(0.02)
    assert cache.get("ttl_key") is None, "TTL expiry failed"
    
    # Test file cache
    file_cache = get_file_cache()
    stats = cache_stats()
    assert "file_cache" in stats, "Cache stats missing file_cache"
    
    print("✓ LRU Cache: All tests passed")
    return True


def test_tool_registry():
    """Test tool registry functionality."""
    print("\n" + "="*60)
    print("Testing Tool Registry...")
    print("="*60)
    
    from agent.tools.tool_registry import (
        get_tool_registry,
        get_tools_for_intent,
        get_tool_openai_schemas,
        ToolCategory,
    )
    
    registry = get_tool_registry()
    
    # Test basic registry
    all_tools = registry.list_all()
    assert len(all_tools) == 26, f"Expected 26 tools, got {len(all_tools)}"
    
    # Test category filtering
    file_tools = registry.by_category(ToolCategory.FILE)
    assert len(file_tools) == 5, f"Expected 5 file tools, got {len(file_tools)}"
    
    # Test intent-based filtering
    doc_tools = get_tools_for_intent("build_document")
    assert any(t.name == "build_deck" for t in doc_tools), "Missing build_deck"
    assert any(t.name == "build_report" for t in doc_tools), "Missing build_report"
    
    # Test OpenAI schema export
    schemas = get_tool_openai_schemas()
    assert len(schemas) == 26, f"Expected 26 schemas, got {len(schemas)}"
    assert all("function" in s for s in schemas), "Missing 'function' in schemas"
    
    # Test intent-specific schemas
    doc_schemas = get_tool_openai_schemas("build_document")
    assert len(doc_schemas) < 26, "Filtered schemas should be smaller than all"
    
    print("✓ Tool Registry: All tests passed")
    return True


async def test_tool_executor():
    """Test parallel tool executor."""
    print("\n" + "="*60)
    print("Testing Tool Executor...")
    print("="*60)
    
    from agent.tools.executor import (
        DefaultToolExecutor,
        ToolCallResult,
        execute_tools_parallel,
    )
    
    # Create executor with mock handlers
    executor = DefaultToolExecutor()
    
    async def mock_handler(args):
        tool_name = args.get("name", "unknown")
        return ToolCallResult(
            tool_name=tool_name,
            success=True,
            output=f"Executed {tool_name}",
        )
    
    # Register handlers
    executor.register_handler("test_tool_1", mock_handler)
    executor.register_handler("test_tool_2", mock_handler)
    executor.register_handler("test_tool_3", mock_handler)
    
    # Test parallel execution
    calls = [
        {"name": "test_tool_1", "args": {"name": "tool1"}},
        {"name": "test_tool_2", "args": {"name": "tool2"}},
        {"name": "test_tool_3", "args": {"name": "tool3"}},
    ]
    
    results = await executor.execute_parallel(calls)
    assert len(results) == 3, f"Expected 3 results, got {len(results)}"
    assert all(r.success for r in results), "Some tools failed"
    
    # Test with max_concurrent limit
    results = await execute_tools_parallel(calls, executor=executor, max_concurrent=2)
    assert len(results) == 3, "Concurrency limit affected result count"
    
    print("✓ Tool Executor: All tests passed")
    return True


def test_context_policy():
    """Test context policy and token budgeting."""
    print("\n" + "="*60)
    print("Testing Context Policy...")
    print("="*60)
    
    from agent.core.context_policy import (
        estimate_tokens,
        get_context_status,
        auto_summarize_history,
        prepare_history_for_api_call,
        build_token_budget_hint,
        validate_history,
        visualize_history,
    )
    
    # Test token estimation
    history = [
        {"role": "system", "content": "You are an assistant."},
        {"role": "user", "content": "Hello, this is a test message with some content."},
        {"role": "assistant", "content": "This is a response."},
    ]
    
    tokens = estimate_tokens(history)
    assert tokens > 0, "Token estimation failed"
    
    # Test context status
    status = get_context_status(history)
    assert status.estimated_tokens > 0, "Status token count invalid"
    assert 0 <= status.percent_full <= 100, "Percent full out of range"
    
    # Test validation
    valid, error = validate_history(history)
    assert valid, f"Valid history failed validation: {error}"
    
    # Test invalid history
    invalid, error = validate_history([])
    assert not invalid, "Empty history should fail validation"
    
    # Test auto-summarize (needs long history)
    long_history = [
        {"role": "system", "content": "You are an assistant."},
    ]
    for i in range(100):
        long_history.append({"role": "user", "content": f"Message {i}: " + "x" * 100})
        long_history.append({"role": "assistant", "content": f"Response {i}: " + "y" * 100})
    
    summarized, was_summarized, kept = auto_summarize_history(long_history)
    assert len(summarized) < len(long_history), "Summarization should reduce message count"
    assert was_summarized, "Should indicate summarization happened"
    
    # Test token budget hint
    hint = build_token_budget_hint(status)
    assert isinstance(hint, str), "Hint should be string"
    
    # Test visualization
    viz = visualize_history(history)
    assert "Conversation History" in viz, "Visualization missing header"
    
    print("✓ Context Policy: All tests passed")
    return True


def test_session_store():
    """Test session persistence."""
    print("\n" + "="*60)
    print("Testing Session Store...")
    print("="*60)
    
    from agent.memory.session_store import SessionStore, SessionMetadata
    
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create store in temp directory
        store = SessionStore(session_dir=".test_session", cwd=tmpdir)
        
        # Test save/load
        history = [
            {"role": "system", "content": "Test system prompt"},
            {"role": "user", "content": "Test user message"},
            {"role": "assistant", "content": "Test assistant response"},
        ]
        
        store.save(history, session_id="test_session_1")
        loaded = store.load()
        
        assert loaded is not None, "Failed to load session"
        assert len(loaded) == len(history), "Session length mismatch"
        assert loaded[1]["content"] == "Test user message", "Message content mismatch"
        
        # Test metadata
        meta = store.get_metadata()
        assert meta is not None, "Metadata not found"
        assert meta.session_id == "test_session_1", "Session ID mismatch"
        assert meta.message_count == 3, "Message count mismatch"
        
        # Test clear
        store.clear()
        assert not store.exists(), "Session should be cleared"
        
        # Test list sessions
        store.save(history)
        sessions = store.list_sessions()
        assert len(sessions) > 0, "No sessions found"
        
    print("✓ Session Store: All tests passed")
    return True


def test_intent_classifier():
    """Test intent classification."""
    print("\n" + "="*60)
    print("Testing Intent Classifier...")
    print("="*60)
    
    from agent.routing.intent_classifier import classify_intent, INTENT_CLASSES
    
    # Test various intents
    test_cases = [
        ("build a PowerPoint presentation", "build_document"),
        ("create a spreadsheet", "data_task"),
        ("review this Python code", "code_task"),
        ("search for Python tutorials", "research"),
        ("read the config file", "file_op"),
        ("run the tests", "system_cmd"),
        ("what is Python?", "query"),
    ]
    
    for query, expected_intent in test_cases:
        result = classify_intent(query)
        assert result.intent == expected_intent, (
            f"Query '{query}' classified as {result.intent}, "
            f"expected {expected_intent}"
        )
        assert 0 <= result.confidence <= 1, "Confidence out of range"
        assert len(result.primary_tools) > 0, "No tools provided"
        assert result.system_addendum, "No system addendum"
    
    # Test that all intent classes are covered
    for intent in INTENT_CLASSES:
        assert any(c[1] == intent for c in test_cases) or intent == "query", (
            f"Intent {intent} not tested"
        )
    
    print("✓ Intent Classifier: All tests passed")
    return True


async def main():
    """Run all tests."""
    print("\n" + "="*60)
    print("JARVIS ENHANCEMENTS VALIDATION TEST")
    print("="*60)
    
    tests = [
        ("LRU Cache", test_lru_cache),
        ("Tool Registry", test_tool_registry),
        ("Tool Executor", test_tool_executor),
        ("Context Policy", test_context_policy),
        ("Session Store", test_session_store),
        ("Intent Classifier", test_intent_classifier),
    ]
    
    passed = 0
    failed = 0
    
    for name, test_fn in tests:
        try:
            if asyncio.iscoroutinefunction(test_fn):
                result = await test_fn()
            else:
                result = test_fn()
            
            if result:
                passed += 1
        except Exception as e:
            print(f"✗ {name}: FAILED")
            print(f"  Error: {e}")
            failed += 1
            import traceback
            traceback.print_exc()
    
    print("\n" + "="*60)
    print(f"RESULTS: {passed} passed, {failed} failed")
    print("="*60 + "\n")
    
    return failed == 0


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
