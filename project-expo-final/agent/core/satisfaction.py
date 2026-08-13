"""
Satisfaction tracker — reward/punishment system.

From chats L320:
  "Rewards like in less chats if response accepted then okay,
   otherwise we will punish — increase severity thinking again.
   By every chat and modification user asks, we begin to find
   the pattern in their instruction and predict their next
   initiatives and do it before, to avoid punishments."

This is NOT a simple counter. It tracks:
  1. Acceptance signals (new unrelated query = last answer accepted)
  2. Correction signals (error fix, depth request, speed complaint)
  3. Severity accumulation (more corrections = deeper thinking)
  4. Pattern detection (predict what user wants next)
  5. Decay (don't punish forever — recent corrections matter more)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from collections import Counter

logger = logging.getLogger(__name__)

# ── Correction patterns (same as store.py but with severity weights) ──

_CORRECTION_WEIGHTS = {
    "error_correction": 3.0,     # worst — agent was wrong
    "wanted_more_depth": 2.0,    # agent was shallow
    "incomplete_work": 2.5,      # agent didn't finish the job
    "too_verbose": 1.0,          # agent over-explained
    "too_slow": 0.5,             # not agent's fault usually
    "style_preference": 0.3,     # minor preference mismatch
}

import json
import urllib.request
from ..config.settings import settings


def classify_severity(text: str) -> tuple[str, float]:
    """Classify a correction and return (type, severity_weight)."""
    from ..memory.store import classify_correction

    correction_type = classify_correction(text)
    
    # Check for incompleteness specifically (L344: "build fully") using semantic LLM
    is_incomplete = False
    if settings.nim.api_keys:
        prompt = (
            "Analyze this user feedback. Is the user complaining that the work is incomplete, missing features, "
            "half-done, or containing placeholders?\n"
            "User text: " + text + "\n\n"
            "Output ONLY 'yes' or 'no'."
        )
        req_data = {
            "model": settings.nim.fast_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.0,
            "max_tokens": 5
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
                ans = json.loads(resp.read().decode("utf-8"))["choices"][0]["message"]["content"].strip().lower()
                is_incomplete = ("yes" in ans)
        except Exception:
            pass

    if is_incomplete:
        return "incomplete_work", _CORRECTION_WEIGHTS["incomplete_work"]

    if correction_type:
        return correction_type, _CORRECTION_WEIGHTS.get(correction_type, 1.0)

    return "", 0.0  # Not a correction


@dataclass
class SatisfactionTracker:
    """Per-session satisfaction tracking with severity accumulation.

    Higher severity_score → deeper thinking, more critique, more validation.
    """
    total_queries: int = 0
    accepted_count: int = 0          # queries where next was unrelated (= accepted)
    correction_count: int = 0        # queries that were corrections
    severity_score: float = 0.0      # accumulated severity (decays)
    correction_types: Counter = field(default_factory=Counter)
    query_topics: list[str] = field(default_factory=list)  # for pattern detection
    _last_query: str = ""
    _last_was_correction: bool = False

    def record_query(self, query: str):
        """Record a new user query and classify it."""
        self.total_queries += 1

        if self._last_query:
            correction_type, weight = classify_severity(query)

            if correction_type:
                # PUNISHMENT — user is correcting us
                self.correction_count += 1
                self.severity_score += weight
                self.correction_types[correction_type] += 1
                self._last_was_correction = True
                logger.info(
                    "Punishment: %s (weight=%.1f, total_severity=%.1f)",
                    correction_type, weight, self.severity_score,
                )
            else:
                # REWARD — new query means last answer was accepted
                if self._last_was_correction:
                    # Recovery: they stopped correcting = issue resolved
                    self.severity_score = max(0, self.severity_score - 1.0)
                self.accepted_count += 1
                self._last_was_correction = False

        # Track topic for pattern detection
        self.query_topics.append(self._extract_topic(query))
        self._last_query = query

        # Decay severity every 5 queries (don't punish forever)
        if self.total_queries % 5 == 0:
            self.severity_score *= 0.7
            logger.debug("Severity decayed to %.1f", self.severity_score)

    def get_thinking_adjustments(self) -> dict:
        """Return adjustments for reasoning.py based on satisfaction.

        These modify ThinkingProfile generation in get_thinking_profile().
        """
        adjustments = {
            "depth_boost": 0,
            "enable_critique": False,
            "enable_self_consistency": False,
            "extra_validation": False,
        }

        if self.severity_score >= 6.0:
            # Very unhappy user — maximum effort
            adjustments["depth_boost"] = 2
            adjustments["enable_critique"] = True
            adjustments["enable_self_consistency"] = True
            adjustments["extra_validation"] = True
        elif self.severity_score >= 3.0:
            # Moderately unhappy — increase depth + critique
            adjustments["depth_boost"] = 1
            adjustments["enable_critique"] = True
        elif self.severity_score >= 1.5:
            # Mild corrections — enable self-consistency
            adjustments["enable_self_consistency"] = True

        # Specific correction patterns
        if self.correction_types.get("incomplete_work", 0) >= 2:
            adjustments["extra_validation"] = True
        if self.correction_types.get("wanted_more_depth", 0) >= 2:
            adjustments["depth_boost"] = max(adjustments["depth_boost"], 1)

        return adjustments

    @property
    def acceptance_rate(self) -> float:
        """Percentage of queries that were accepted (not corrected)."""
        if self.total_queries <= 1:
            return 1.0
        return self.accepted_count / max(1, self.total_queries - 1)

    def predict_next_need(self) -> str:
        """Predict what the user might need next based on query patterns.

        From chats L320: "find the pattern in their instruction
        and predict their next initiatives"
        """
        if len(self.query_topics) < 2:
            return ""

        recent = self.query_topics[-3:]

        # Detect escalation pattern: overview → detail → deeper
        if len(recent) >= 2:
            # Same topic repeated with corrections = they want deeper
            if recent[-1] == recent[-2]:
                return f"deeper analysis of {recent[-1]}"

        # Detect sequence pattern: if topics form a logical chain
        if len(recent) >= 3:
            # All different = exploring broadly
            if len(set(recent)) == len(recent):
                return ""  # No prediction for scattered queries

        return ""

    def _extract_topic(self, query: str) -> str:
        """Extract a rough topic from a query (for pattern detection)."""
        # Remove common words, keep nouns/verbs
        words = re.findall(r'\b[a-zA-Z]{4,}\b', query.lower())
        # Remove very common words
        stopwords = {"what", "how", "does", "this", "that", "with", "from",
                     "about", "which", "where", "when", "have", "make",
                     "create", "build", "want", "need", "like", "will",
                     "should", "could", "would", "please", "help", "tell"}
        meaningful = [w for w in words if w not in stopwords]
        return " ".join(meaningful[:3]) if meaningful else "general"
