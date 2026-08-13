"""
Query-level semantic cache — embed the query, cosine-match against
previous queries, serve cached result if similarity > threshold.

This is NOT just string-matching: "how do transformers work" and
"explain transformer architecture" should hit the same cache entry.

TTL: 15 minutes default. Never caches errors or empty results.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Optional

from ..config.settings import settings


@dataclass
class CacheEntry:
    query: str
    query_vec: list[float]
    result: dict
    timestamp: float


class SemanticCache:
    def __init__(
        self,
        threshold: float = 0,
        ttl: int = 0,
        max_entries: int = 200,
    ):
        self._threshold = threshold or settings.semantic_cache_threshold
        self._ttl = ttl or settings.semantic_cache_ttl
        self._max = max_entries
        self._entries: list[CacheEntry] = []

    @staticmethod
    def _cosine(a: list[float], b: list[float]) -> float:
        if not a or not b:
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a)) or 1.0
        nb = math.sqrt(sum(x * x for x in b)) or 1.0
        return dot / (na * nb)

    def get(self, query_vec: list[float]) -> Optional[dict]:
        """Return cached result if any stored query is semantically similar
        enough (cosine > threshold) and not expired."""
        now = time.time()
        best_sim = 0.0
        best_entry = None

        for entry in self._entries:
            if now - entry.timestamp > self._ttl:
                continue
            sim = self._cosine(query_vec, entry.query_vec)
            if sim > best_sim:
                best_sim = sim
                best_entry = entry

        if best_entry and best_sim >= self._threshold:
            return best_entry.result
        return None

    def set(self, query: str, query_vec: list[float], result: dict):
        """Store a query result. Evicts oldest entries if over capacity."""
        if not result:
            return

        self._entries.append(CacheEntry(
            query=query,
            query_vec=query_vec,
            result=result,
            timestamp=time.time(),
        ))

        # Evict expired + oldest
        now = time.time()
        self._entries = [
            e for e in self._entries
            if now - e.timestamp < self._ttl
        ]
        if len(self._entries) > self._max:
            self._entries = self._entries[-self._max:]

    def clear(self):
        self._entries.clear()


# Module-level singleton
_cache: Optional[SemanticCache] = None


def get_semantic_cache() -> SemanticCache:
    global _cache
    if _cache is None:
        _cache = SemanticCache()
    return _cache
