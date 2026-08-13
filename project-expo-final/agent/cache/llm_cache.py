"""
LLM response cache — SHA-256(prompt|model|system) → response.

Two layers, from github_researchtool.py's proven pattern:
  1. In-memory LRU dict (fast, bounded)
  2. SQLite WAL persistent (survives restarts)

Never caches errors or empty responses — a cached miss is worse than
an uncached retry.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from collections import OrderedDict
from typing import Optional

from ..config.settings import settings


class LLMCache:
    def __init__(self, max_memory: int = 0, ttl: int = 0, db_path: str = ""):
        self._max = max_memory or settings.llm_cache_max
        self._ttl = ttl or settings.llm_cache_ttl
        self._mem: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self._lock = threading.Lock()

        db_dir = db_path or settings.cache_dir
        os.makedirs(db_dir, exist_ok=True)
        self._db_path = os.path.join(db_dir, "llm_cache.db")
        self._db = sqlite3.connect(self._db_path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS llm_cache "
            "(key TEXT PRIMARY KEY, value TEXT, ts REAL)"
        )
        self._db.commit()

    @staticmethod
    def _hash(prompt: str, model: str, system: str = "") -> str:
        raw = f"{prompt}|{model}|{system}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def get(self, prompt: str, model: str, system: str = "") -> Optional[str]:
        key = self._hash(prompt, model, system)
        now = time.time()

        # Layer 1: in-memory
        with self._lock:
            if key in self._mem:
                val, ts = self._mem[key]
                if now - ts < self._ttl:
                    self._mem.move_to_end(key)
                    return val
                else:
                    del self._mem[key]

        # Layer 2: SQLite
        try:
            row = self._db.execute(
                "SELECT value, ts FROM llm_cache WHERE key=?", (key,)
            ).fetchone()
            if row and now - row[1] < self._ttl:
                val = row[0]
                with self._lock:
                    self._mem[key] = (val, row[1])
                    self._evict()
                return val
        except Exception:
            pass

        return None

    def set(self, prompt: str, model: str, response: str, system: str = ""):
        if not response or not response.strip():
            return  # never cache empty

        key = self._hash(prompt, model, system)
        now = time.time()

        with self._lock:
            self._mem[key] = (response, now)
            self._evict()

        try:
            self._db.execute(
                "INSERT OR REPLACE INTO llm_cache (key, value, ts) VALUES (?, ?, ?)",
                (key, response, now),
            )
            self._db.commit()
        except Exception:
            pass

    def _evict(self):
        while len(self._mem) > self._max:
            self._mem.popitem(last=False)


# Module-level singleton
_cache: Optional[LLMCache] = None


def get_llm_cache() -> LLMCache:
    global _cache
    if _cache is None:
        _cache = LLMCache()
    return _cache
