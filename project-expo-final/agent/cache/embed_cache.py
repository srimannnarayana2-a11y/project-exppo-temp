"""
Embedding vector cache — SHA-256(content) → normalized vector.

Same two-layer pattern as llm_cache.py (in-memory + SQLite WAL).
Saves expensive embedding API calls: same content = same vector, never
re-embed. From github_researchtool.py's _embed_batch_cached pattern.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from typing import Optional

from ..config.settings import settings


class EmbedCache:
    def __init__(self, ttl: int = 0, db_path: str = ""):
        self._ttl = ttl or settings.embed_cache_ttl
        self._mem: dict[str, list[float]] = {}
        self._lock = threading.Lock()

        db_dir = db_path or settings.cache_dir
        os.makedirs(db_dir, exist_ok=True)
        self._db_path = os.path.join(db_dir, "embed_cache.db")
        self._db = sqlite3.connect(self._db_path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS embed_cache "
            "(key TEXT PRIMARY KEY, vec TEXT, ts REAL)"
        )
        self._db.commit()

    @staticmethod
    def _hash(text: str) -> str:
        return hashlib.sha256(text.strip().encode()).hexdigest()

    def get(self, text: str) -> Optional[list[float]]:
        key = self._hash(text)
        now = time.time()

        with self._lock:
            if key in self._mem:
                return self._mem[key]

        try:
            row = self._db.execute(
                "SELECT vec, ts FROM embed_cache WHERE key=?", (key,)
            ).fetchone()
            if row and now - row[1] < self._ttl:
                vec = json.loads(row[0])
                with self._lock:
                    self._mem[key] = vec
                return vec
        except Exception:
            pass

        return None

    def get_batch(self, texts: list[str]) -> tuple[list[Optional[list[float]]], list[int]]:
        """Returns (results, uncached_indices).
        results[i] is the cached vector or None if not cached.
        uncached_indices lists which positions need API calls."""
        results: list[Optional[list[float]]] = []
        uncached: list[int] = []
        for i, t in enumerate(texts):
            v = self.get(t)
            results.append(v)
            if v is None:
                uncached.append(i)
        return results, uncached

    def set(self, text: str, vec: list[float]):
        key = self._hash(text)
        now = time.time()

        with self._lock:
            self._mem[key] = vec

        try:
            self._db.execute(
                "INSERT OR REPLACE INTO embed_cache (key, vec, ts) VALUES (?, ?, ?)",
                (key, json.dumps(vec), now),
            )
            self._db.commit()
        except Exception:
            pass

    def set_batch(self, texts: list[str], vecs: list[list[float]]):
        for t, v in zip(texts, vecs):
            self.set(t, v)


# Module-level singleton
_cache: Optional[EmbedCache] = None


def get_embed_cache() -> EmbedCache:
    global _cache
    if _cache is None:
        _cache = EmbedCache()
    return _cache
