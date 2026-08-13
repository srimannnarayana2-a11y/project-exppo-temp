"""
Per-session memory store with correction-pattern tracking.

From chats L327: classify each follow-up as new-request vs correction,
accumulate per-category effort_bias (not one global counter), decay
over time/topic so it doesn't overcorrect.

This feeds into entry_gate/reasoning via EffortBias.
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from typing import Optional

from ..core.reasoning import EffortBias

logger = logging.getLogger(__name__)

import json
import urllib.request
from ..config.settings import settings
def classify_correction(text: str) -> Optional[str]:
    """Classify a follow-up message as a correction type, or None if it's a new request.
    Uses a fast, synchronous LLM call to capture nuance without breaking compatibility.
    """
    if not settings.nim.api_keys:
        return None

    prompt = (
        "Analyze this user feedback to an AI. Is it a correction/complaint or a new topic?\n"
        "Categories:\n"
        "- error_correction: 'that's wrong', 'fix this', 'incorrect'\n"
        "- wanted_more_depth: 'explain more', 'expand on that', 'not detailed enough'\n"
        "- too_slow: 'faster', 'taking too long'\n"
        "- too_verbose: 'shorter', 'too long', 'summarize'\n"
        "- none: standard new question or unrelated\n\n"
        "User text: " + text + "\n\n"
        "Output ONLY the category name exactly as written above, or 'none'."
    )

    req_data = {
        "model": settings.nim.fast_model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
        "max_tokens": 10
    }
    
    req = urllib.request.Request(
        f"{settings.nim.base_url}/chat/completions",
        data=json.dumps(req_data).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.nim.api_keys[0]}",
            "Content-Type": "application/json"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            ans = data["choices"][0]["message"]["content"].strip().lower()
            if ans in ("error_correction", "wanted_more_depth", "too_slow", "too_verbose"):
                return ans
    except Exception as e:
        logger.debug("Sync LLM classify_correction failed: %s", e)
    
    return None


@dataclass
class SessionMemory:
    """One session's accumulated context + correction tracking."""
    session_id: str
    turns: list[dict] = field(default_factory=list)
    user_preferences: dict = field(default_factory=dict)
    corrections: list[str] = field(default_factory=list)
    effort_bias: EffortBias = field(default_factory=EffortBias)
    _compacted_summary: str = ""  # compressed old turns

    # ── Context window compaction (prevents overflow) ──
    MAX_TURNS_BEFORE_COMPACT = 20  # compact after this many turns
    KEEP_RECENT_TURNS = 6          # always keep the most recent N turns

    def add_turn(self, role: str, content: str):
        self.turns.append({"role": role, "content": content})

        # Auto-classify user turns as potential corrections
        if role == "user" and len(self.turns) > 2:
            correction_type = classify_correction(content)
            if correction_type:
                self.effort_bias.record_correction(correction_type)
                self.corrections.append(f"{correction_type}: {content[:100]}")
                logger.info("Session %s: correction detected (%s)", self.session_id, correction_type)

        # Decay biases every 5 turns to prevent runaway accumulation
        if len(self.turns) % 5 == 0:
            self.effort_bias.apply_decay()

        # Context window compaction: compress old turns into summary
        if len(self.turns) > self.MAX_TURNS_BEFORE_COMPACT:
            self._compact_old_turns()

    def _compact_old_turns(self):
        """Compress old turns into a summary, keeping recent ones intact.

        This is how the agent maintains memory without blowing up context:
        old exchanges get summarized into key facts/decisions/preferences.
        """
        old_turns = self.turns[:-self.KEEP_RECENT_TURNS]
        recent_turns = self.turns[-self.KEEP_RECENT_TURNS:]

        # Build summary of old turns
        summary_parts = []
        if self._compacted_summary:
            summary_parts.append(self._compacted_summary)

        for turn in old_turns:
            role = turn['role']
            content = turn['content'][:150]
            if role == 'user':
                summary_parts.append(f"User asked: {content}")
            else:
                # Only keep key info from assistant responses
                summary_parts.append(f"Agent answered about: {content[:80]}")

        self._compacted_summary = " | ".join(summary_parts[-15:])  # Keep last 15 summaries
        self.turns = recent_turns
        logger.debug("Session %s: compacted %d old turns", self.session_id, len(old_turns))

    def add_correction(self, correction: str):
        self.corrections.append(correction)

    def get_context(self, max_turns: int = 5) -> list[str]:
        """Returns recent context strings for the clarify module."""
        context = []

        # Include compacted summary (compressed old context)
        if self._compacted_summary:
            context.append(f"session_history: {self._compacted_summary[:500]}")

        for turn in self.turns[-max_turns:]:
            context.append(f"{turn['role']}: {turn['content'][:200]}")
        for corr in self.corrections[-3:]:
            context.append(f"correction: {corr}")
        for k, v in self.user_preferences.items():
            context.append(f"preference: {k}={v}")
        return context


class MemoryStore:
    """In-memory session store. Thread-safe for asyncio (single-threaded)."""

    def __init__(self):
        self._sessions: dict[str, SessionMemory] = {}

    def get_or_create(self, session_id: str) -> SessionMemory:
        if session_id not in self._sessions:
            self._sessions[session_id] = SessionMemory(session_id=session_id)
        return self._sessions[session_id]

    def get(self, session_id: str) -> Optional[SessionMemory]:
        return self._sessions.get(session_id)

    def delete(self, session_id: str):
        self._sessions.pop(session_id, None)

    @property
    def active_sessions(self) -> int:
        return len(self._sessions)


_store: Optional[MemoryStore] = None


def get_memory_store() -> MemoryStore:
    global _store
    if _store is None:
        _store = MemoryStore()
    return _store
