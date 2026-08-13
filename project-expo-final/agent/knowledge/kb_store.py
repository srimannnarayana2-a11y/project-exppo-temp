"""
In-memory vector knowledge base store with numpy-backed cosine search.

Simple but fast — all vectors live in memory as a numpy matrix. Supports:
  - add(chunks) → embed and store
  - query(embedding, top_k) → cosine similarity search
  - Subtree filtering by source_url prefix (for folder-scoped queries)
  - Persistent save/load to disk (JSON + numpy .npy)

Upgradeable to a proper vector DB (Qdrant, pgvector) later without
changing the interface.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Optional

import numpy as np

from ..config.settings import settings
from ..blocks.semantic.types import Chunk, Learning

logger = logging.getLogger(__name__)


@dataclass
class KBEntry:
    text: str
    source_url: str
    title: str
    embedding: list[float]


class KBStore:
    """Numpy-backed in-memory vector store."""

    def __init__(self, persist_dir: str = ""):
        self._entries: list[KBEntry] = []
        self._matrix: Optional[np.ndarray] = None  # N x dim matrix
        self._dirty = False
        self._persist_dir = persist_dir or settings.kb_dir

    @property
    def size(self) -> int:
        return len(self._entries)

    def add(self, entries: list[KBEntry]):
        """Add pre-embedded entries. Call rebuild_matrix() after bulk adds."""
        self._entries.extend(entries)
        self._dirty = True

    def add_chunks(self, chunks: list[Chunk]):
        """Add Chunk objects (must have .embedding populated)."""
        for c in chunks:
            if c.embedding:
                self._entries.append(KBEntry(
                    text=c.text,
                    source_url=c.source_url,
                    title=c.title,
                    embedding=c.embedding,
                ))
        self._dirty = True

    def rebuild_matrix(self):
        """Rebuild the numpy matrix from entries. Call after bulk adds."""
        if not self._entries:
            self._matrix = None
            return
        self._matrix = np.array(
            [e.embedding for e in self._entries], dtype=np.float32
        )
        # L2 normalize rows
        norms = np.linalg.norm(self._matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        self._matrix = self._matrix / norms
        self._dirty = False

    def query(
        self,
        query_vec: list[float],
        top_k: int = 8,
        source_prefix: str = "",
    ) -> list[Chunk]:
        """Cosine similarity search. Returns top_k Chunk objects."""
        if not self._entries or self._matrix is None:
            if self._dirty:
                self.rebuild_matrix()
            if self._matrix is None:
                return []

        qv = np.array(query_vec, dtype=np.float32)
        norm = np.linalg.norm(qv)
        if norm > 0:
            qv = qv / norm

        # Dot product = cosine similarity (both normalized)
        scores = self._matrix @ qv

        # Source prefix filter
        if source_prefix:
            mask = np.array([
                e.source_url.startswith(source_prefix) for e in self._entries
            ])
            scores = scores * mask

        # Top-k
        top_indices = np.argsort(scores)[-top_k:][::-1]

        results = []
        for idx in top_indices:
            if scores[idx] <= 0:
                continue
            e = self._entries[idx]
            results.append(Chunk(
                text=e.text,
                source_url=e.source_url,
                title=e.title,
                score=float(scores[idx]),
                embedding=e.embedding,
            ))

        return results

    def save(self):
        """Persist to disk."""
        os.makedirs(self._persist_dir, exist_ok=True)

        meta = [{
            "text": e.text, "source_url": e.source_url, "title": e.title,
        } for e in self._entries]
        with open(os.path.join(self._persist_dir, "kb_meta.json"), "w") as f:
            json.dump(meta, f)

        if self._matrix is not None:
            np.save(os.path.join(self._persist_dir, "kb_vectors.npy"), self._matrix)

        logger.info("KB saved: %d entries to %s", len(self._entries), self._persist_dir)

    def load(self) -> bool:
        """Load from disk. Returns True if loaded successfully."""
        meta_path = os.path.join(self._persist_dir, "kb_meta.json")
        vec_path = os.path.join(self._persist_dir, "kb_vectors.npy")

        if not os.path.exists(meta_path):
            return False

        try:
            with open(meta_path) as f:
                meta = json.load(f)

            if os.path.exists(vec_path):
                matrix = np.load(vec_path)
            else:
                return False

            self._entries = []
            for i, m in enumerate(meta):
                self._entries.append(KBEntry(
                    text=m["text"],
                    source_url=m["source_url"],
                    title=m.get("title", ""),
                    embedding=matrix[i].tolist() if i < len(matrix) else [],
                ))

            self._matrix = matrix
            self._dirty = False
            logger.info("KB loaded: %d entries from %s", len(self._entries), self._persist_dir)
            return True
        except Exception as e:
            logger.warning("KB load failed: %s", e)
            return False

    def clear(self):
        self._entries.clear()
        self._matrix = None
        self._dirty = False


# Module-level singleton
_store: Optional[KBStore] = None


def get_kb_store() -> KBStore:
    global _store
    if _store is None:
        _store = KBStore()
        _store.load()  # Try to load persisted data
    return _store
