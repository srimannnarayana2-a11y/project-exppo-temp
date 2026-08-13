"""
Adaptive thinking — decides how deep the system should reason.

TWO sources of adaptation (from chats L320, L344):

1. GATE MODE → base parameters (depth, budget, feature flags)
2. PROMPT SPECIFICITY → depth calibration
   - Expert prompt ("implement OAuth2 PKCE with S256 code verifier") → deeper
   - Casual prompt ("what is oauth") → broader, more accessible
   This is NOT a gate decision — it's a presentation/depth decision

3. EFFORT BIAS from correction history (L327)
   - If user keeps asking for more depth → increase thinking
   - If user keeps correcting errors → increase self-consistency
   - Per-category, decaying, not a single accumulating counter

Also: classify prompt specificity using cheap heuristics (no LLM call).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..config.budgets import DEFAULT_MAX_DEPTH, GLOBAL_BUDGET_S


@dataclass
class ThinkingProfile:
    """Execution parameters derived from query complexity + user history."""
    max_depth: int
    budget_s: float
    use_deep_propositions: bool
    use_critique: bool
    use_multi_query_expansion: bool
    prompt_specificity: str          # "expert" | "standard" | "casual"
    self_consistency_calls: int      # 1 = normal, 2 = anti-sycophancy check


import json
import urllib.request
from ..config.settings import settings


def classify_prompt_specificity(query: str) -> str:
    """Classify whether the query comes from an expert or casual user.
    Uses a fast synchronous LLM call to evaluate semantic complexity.
    """
    word_count = len(query.split())
    if not settings.nim.api_keys:
        return "expert" if word_count > 30 else "standard"

    prompt = (
        "Analyze this user query to an AI. Classify the user's technical expertise level.\n"
        "Categories:\n"
        "- expert: uses advanced technical jargon, specific architectures, code patterns, or asks for complex implementations\n"
        "- casual: simple, high-level questions (e.g. 'what is python', 'explain X')\n"
        "- standard: typical queries that don't fit the extremes\n\n"
        "User query: " + query + "\n\n"
        "Output ONLY the category name."
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
            if ans in ("expert", "casual", "standard"):
                return ans
    except Exception:
        pass

    return "expert" if word_count > 30 else "standard"


# ── Effort bias from correction history ──

@dataclass
class EffortBias:
    """Per-category effort bias with decay. From chat L327:
    'per-category effort_bias, decaying, not a single accumulating counter'"""
    depth_bias: float = 0.0      # User wants MORE depth (positive = deeper)
    accuracy_bias: float = 0.0   # User keeps correcting errors (positive = more careful)
    speed_bias: float = 0.0      # User wants FASTER responses (positive = shallower)

    def apply_decay(self, factor: float = 0.85):
        """Decay biases over time/turns so they don't overcorrect."""
        self.depth_bias *= factor
        self.accuracy_bias *= factor
        self.speed_bias *= factor

    def record_correction(self, correction_type: str):
        """Record a user correction and adjust the right bias."""
        if correction_type == "wanted_more_depth":
            self.depth_bias += 1.0
        elif correction_type == "error_correction":
            self.accuracy_bias += 1.0
        elif correction_type == "too_slow":
            self.speed_bias += 1.0
        elif correction_type == "too_verbose":
            self.depth_bias -= 0.5


# ── Profile computation ──

_BASE_PROFILES = {
    "PARAMETRIC": {
        "max_depth": 0,
        "budget_s": 5.0,
        "use_deep_propositions": False,
        "use_critique": False,
        "use_multi_query_expansion": False,
    },
    "SEMANTIC": {
        "max_depth": DEFAULT_MAX_DEPTH,
        "budget_s": GLOBAL_BUDGET_S,
        "use_deep_propositions": True,
        "use_critique": False,
        "use_multi_query_expansion": True,
    },
    "CODE": {
        "max_depth": 2,
        "budget_s": GLOBAL_BUDGET_S,
        "use_deep_propositions": True,
        "use_critique": False,
        "use_multi_query_expansion": True,
    },
    "HYBRID": {
        "max_depth": DEFAULT_MAX_DEPTH,
        "budget_s": GLOBAL_BUDGET_S + 10.0,
        "use_deep_propositions": True,
        "use_critique": True,
        "use_multi_query_expansion": True,
    },
}


def get_thinking_profile(
    gate_mode: str,
    query: str = "",
    effort_bias: EffortBias = None,
) -> ThinkingProfile:
    """Build execution parameters from gate mode + prompt specificity + user history.

    Three adaptation layers:
    1. Gate mode → base parameters
    2. Prompt specificity → presentation depth
    3. Effort bias → per-user adjustments from correction history
    """
    base = _BASE_PROFILES.get(gate_mode, _BASE_PROFILES["SEMANTIC"]).copy()
    specificity = classify_prompt_specificity(query) if query else "standard"

    # Apply effort bias
    self_consistency = 1
    if effort_bias:
        # Depth bias: user wants more/less depth
        if effort_bias.depth_bias > 1.0:
            base["max_depth"] = min(base["max_depth"] + 1, 5)
            base["use_critique"] = True
        elif effort_bias.depth_bias < -1.0:
            base["max_depth"] = max(base["max_depth"] - 1, 0)

        # Accuracy bias: user keeps finding errors → more self-consistency
        if effort_bias.accuracy_bias > 1.0:
            self_consistency = 2
            base["use_critique"] = True

        # Speed bias: user wants faster
        if effort_bias.speed_bias > 1.0:
            base["budget_s"] = max(base["budget_s"] * 0.7, 5.0)
            base["max_depth"] = max(base["max_depth"] - 1, 0)

    # Expert prompts get deeper treatment
    if specificity == "expert" and base["max_depth"] > 0:
        base["max_depth"] = min(base["max_depth"] + 1, 5)
        base["use_deep_propositions"] = True

    return ThinkingProfile(
        max_depth=base["max_depth"],
        budget_s=base["budget_s"],
        use_deep_propositions=base["use_deep_propositions"],
        use_critique=base["use_critique"],
        use_multi_query_expansion=base["use_multi_query_expansion"],
        prompt_specificity=specificity,
        self_consistency_calls=self_consistency,
    )
