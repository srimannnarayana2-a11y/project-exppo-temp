"""
Context Policy — Token Budgeting and Auto-Summarization

From Jarvis's `contextPolicy.ts`:
  - Estimates token count from conversation history
  - Triggers auto-summarization when context > 50K tokens
  - Compresses older turns into summary while keeping recent turns verbatim
  - Prevents silent context overflows (the LLM complains, but too late)

Industry patterns:
  - Anthropic's Claude Code: sliding window + summary injection
  - OpenAI's swarm: per-agent context trimming
  - LangChain: ConversationSummaryBufferMemory

Usage:
    from agent.core.context_policy import get_context_status, auto_summarize_history
    
    status = get_context_status(history)
    if status['should_summarize']:
        history = auto_summarize_history(history)
        print(status['warning'])  # e.g., "Context 92% full"
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────

# Approximate tokens: 4 characters ≈ 1 token (conservative estimate)
CHARS_PER_TOKEN = 4

# Start summarizing when estimated context exceeds this token count
# Most NVIDIA NIM models support 32K–128K context. 50K is conservative.
SUMMARIZE_THRESHOLD_TOKENS = 50_000

# How many recent turns to ALWAYS keep verbatim (don't summarize)
KEEP_RECENT_TURNS = 6

# Token estimate buffer (safety margin to avoid hitting context limit)
SAFETY_MARGIN_TOKENS = 5_000


# ─── Status Types ─────────────────────────────────────────────────────────────

@dataclass
class ContextStatus:
    """Status of conversation context."""
    estimated_tokens: int
    percent_full: int           # 0–100
    should_summarize: bool
    warning: str = ""
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "estimated_tokens": self.estimated_tokens,
            "percent_full": self.percent_full,
            "should_summarize": self.should_summarize,
            "warning": self.warning,
        }


# ─── Token Estimation ─────────────────────────────────────────────────────────

def estimate_tokens(history: List[Dict[str, Any]]) -> int:
    """
    Estimate token count in conversation history.
    
    Conservative estimate: 4 chars ≈ 1 token
    Includes message content, tool calls, and all metadata.
    
    Args:
        history: List of message dicts (role, content, tool_calls, etc.)
    
    Returns:
        Estimated token count
    """
    total_chars = 0
    
    for msg in history:
        # Content
        content = msg.get("content", "")
        if content:
            total_chars += len(content)
        
        # Tool calls (if any)
        if "tool_calls" in msg:
            total_chars += len(json.dumps(msg["tool_calls"]))
        
        # Tool results
        if "name" in msg or "tool_call_id" in msg:
            total_chars += len(msg.get("name", "")) + len(msg.get("tool_call_id", ""))
    
    return max(1, total_chars // CHARS_PER_TOKEN)


def get_context_status(history: List[Dict[str, Any]]) -> ContextStatus:
    """
    Analyze context usage and suggest actions.
    
    Args:
        history: Conversation history
    
    Returns:
        ContextStatus with token estimates and warnings
    """
    estimated_tokens = estimate_tokens(history)
    percent_full = int((estimated_tokens / SUMMARIZE_THRESHOLD_TOKENS) * 100)
    should_summarize = estimated_tokens > SUMMARIZE_THRESHOLD_TOKENS
    
    warning = ""
    if percent_full > 90:
        warning = (
            f"⚠ Context is {percent_full}% full "
            f"(est. {estimated_tokens:,} tokens). "
            f"Auto-summarizing older turns."
        )
    elif percent_full > 70:
        warning = (
            f"ℹ Context is {percent_full}% full "
            f"(est. {estimated_tokens:,} tokens)."
        )
    
    return ContextStatus(
        estimated_tokens=estimated_tokens,
        percent_full=percent_full,
        should_summarize=should_summarize,
        warning=warning,
    )


# ─── Auto-Summarization ───────────────────────────────────────────────────────

def auto_summarize_history(
    history: List[Dict[str, Any]],
    keep_recent: int = KEEP_RECENT_TURNS,
) -> tuple[List[Dict[str, Any]], bool, int]:
    """
    Compress older turns into a summary when context is too large.
    
    Always keeps:
      - messages[0] = system prompt (never summarized)
      - the last `keep_recent` non-system messages verbatim
    
    Compresses:
      - All messages between system prompt and kept recent turns
      - Summarizes turns 1, 2, 3, ... keeping only key facts
    
    Args:
        history: Full conversation history
        keep_recent: Number of recent turns to keep verbatim
    
    Returns:
        (summarized_history, was_summarized, kept_count)
    """
    if len(history) <= keep_recent + 2:
        # Not enough messages to compress
        return history, False, len(history)
    
    # Extract system message (always kept)
    system_msg = history[0] if history and history[0].get("role") == "system" else None
    non_system = history[1:] if system_msg else history
    
    # Split: older messages to summarize vs. recent messages to keep verbatim
    recent_count = min(keep_recent, len(non_system))
    to_summarize = non_system[:-recent_count] if recent_count > 0 else non_system
    to_keep = non_system[-recent_count:] if recent_count > 0 else []
    
    if not to_summarize:
        return history, False, len(history)
    
    # Build text summary of compressed turns
    lines: List[str] = [
        "[Earlier conversation summary — auto-compressed to save context]",
        "",
    ]
    
    turn_num = 0
    for msg in to_summarize:
        role = msg.get("role", "")
        content = msg.get("content", "")
        
        if role == "user":
            turn_num += 1
            # Keep first 300 chars of user query
            snippet = content[:300].replace("\n", " ")
            lines.append(f"Turn {turn_num} — User: {snippet}")
        
        elif role == "assistant" and content and content.strip():
            # Keep first 200 chars of assistant response
            snippet = content[:200].replace("\n", " ")
            lines.append(f"  → Assistant: {snippet}")
        
        elif role == "assistant" and msg.get("tool_calls"):
            # Log which tools were used
            tool_names = [
                call.get("function", {}).get("name", "unknown")
                for call in msg["tool_calls"]
            ]
            lines.append(f"  → Used tools: {', '.join(tool_names)}")
    
    lines.append("")
    summary_text = "\n".join(lines)
    
    # Create summary message
    summary_msg: Dict[str, Any] = {
        "role": "system",
        "content": summary_text,
    }
    
    # Reconstruct history: system + summary + recent turns
    new_history: List[Dict[str, Any]] = []
    if system_msg:
        new_history.append(system_msg)
    new_history.append(summary_msg)
    new_history.extend(to_keep)
    
    kept_count = len(new_history)
    original_count = len(history)
    reduction_pct = int((1 - kept_count / original_count) * 100)
    
    logger.info(
        f"Auto-summarized history: {original_count} → {kept_count} messages "
        f"({reduction_pct}% reduction)"
    )
    
    return new_history, True, kept_count


# ─── Token Budget Hints ────────────────────────────────────────────────────────

def build_token_budget_hint(status: ContextStatus) -> str:
    """
    Build a system prompt hint about token budget for the LLM.
    
    Tells the model to be concise if context is filling up.
    """
    percent = status.percent_full
    
    if percent > 90:
        return (
            "TOKEN BUDGET CRITICAL: Context is nearly full. "
            "Be extremely concise. Use bullet points. Avoid long explanations. "
            "Focus only on essential information."
        )
    elif percent > 75:
        return (
            "TOKEN BUDGET HIGH: Context is getting full. "
            "Be concise where possible. Prioritize essential facts."
        )
    elif percent > 50:
        return (
            "TOKEN BUDGET MEDIUM: Keep responses reasonably concise. "
            "No need to over-compress."
        )
    else:
        return ""


def build_context_summary_prompt(status: ContextStatus) -> str:
    """
    Build a system addendum about current context state.
    
    Helps the model understand how much context it has left.
    """
    tokens = status.estimated_tokens
    percent = status.percent_full
    
    return (
        f"Current conversation context: ~{tokens:,} tokens ({percent}% of budget). "
        f"You have ~{SUMMARIZE_THRESHOLD_TOKENS - tokens:,} tokens of room left."
    )


# ─── History Preparation for API Calls ────────────────────────────────────────

def prepare_history_for_api_call(
    history: List[Dict[str, Any]],
    auto_summarize: bool = True,
) -> List[Dict[str, Any]]:
    """
    Prepare conversation history for API call.
    
    - Checks token budget
    - Auto-summarizes if enabled and over threshold
    - Logs status
    
    Args:
        history: Full conversation history
        auto_summarize: Whether to summarize if over threshold
    
    Returns:
        Prepared history (possibly summarized)
    """
    status = get_context_status(history)
    
    if status.warning:
        logger.warning(status.warning)
    
    if auto_summarize and status.should_summarize:
        history, _, _ = auto_summarize_history(history)
    
    return history


# ─── Validation & Health Checks ───────────────────────────────────────────────

def validate_history(history: List[Dict[str, Any]]) -> tuple[bool, str]:
    """
    Validate conversation history format.
    
    Returns:
        (is_valid, error_message)
    """
    if not isinstance(history, list):
        return False, "History must be a list"
    
    if not history:
        return False, "History cannot be empty"
    
    # Check first message is system
    if history[0].get("role") != "system":
        return False, "First message must have role='system'"
    
    # Check all messages have required fields
    for i, msg in enumerate(history):
        if "role" not in msg or "content" not in msg:
            return False, f"Message {i} missing 'role' or 'content'"
        
        valid_roles = {"system", "user", "assistant", "tool"}
        if msg["role"] not in valid_roles:
            return False, f"Message {i} has invalid role: {msg['role']}"
    
    return True, ""


# ─── Debugging & Visualization ────────────────────────────────────────────────

def visualize_history(history: List[Dict[str, Any]], max_messages: int = 20) -> str:
    """
    Create a text visualization of conversation history.
    
    Useful for debugging and understanding context.
    """
    lines = [
        "═" * 60,
        "Conversation History Visualization",
        "═" * 60,
    ]
    
    status = get_context_status(history)
    lines.append(f"Total messages: {len(history)}")
    lines.append(f"Estimated tokens: {status.estimated_tokens:,}")
    lines.append(f"Context usage: {status.percent_full}%")
    lines.append("")
    
    # Show messages
    shown = 0
    for i, msg in enumerate(history):
        if max_messages and shown >= max_messages:
            lines.append(f"... and {len(history) - shown} more messages")
            break
        
        role = msg.get("role", "?")
        content = msg.get("content", "")[:80].replace("\n", " ")
        
        if role == "system":
            lines.append(f"[{i:3d}] SYSTEM: {content}...")
        elif role == "user":
            lines.append(f"[{i:3d}] USER:   {content}...")
        elif role == "assistant":
            if msg.get("tool_calls"):
                tools = [t.get("function", {}).get("name") for t in msg["tool_calls"]]
                lines.append(f"[{i:3d}] ASST:   [tool calls: {', '.join(tools)}]")
            else:
                lines.append(f"[{i:3d}] ASST:   {content}...")
        elif role == "tool":
            lines.append(f"[{i:3d}] TOOL:   {content}...")
        
        shown += 1
    
    lines.append("═" * 60)
    return "\n".join(lines)
