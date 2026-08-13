"""
Hybrid reranking — RRF (Reciprocal Rank Fusion) of dense cosine + BM25.

From github_researchtool.py's proven rerank_rrf, adapted for the semantic
block's Chunk type. RRF is parameter-free (unlike linear weighted fusion
which needs per-dataset tuning) — the only constant is k=60 (standard).

Optional NVIDIA rerank API acceleration: if available, uses logit-threshold
filtering to instantly purge noise before RRF.
"""

from __future__ import annotations

import math
import logging
from typing import Optional

from ...llm.client import NIMClient, get_client
from ...config.budgets import RRF_K, TOP_K_RERANK, RERANK_LOGIT_CUTOFF
from .types import Chunk

logger = logging.getLogger(__name__)


def cosine_sim(a: list[float], b: list[float]) -> float:
    """Dot product of L2-normalized vectors."""
    if not a or not b:
        return 0.0
    min_len = min(len(a), len(b))
    dot = sum(a[i] * b[i] for i in range(min_len))
    na = math.sqrt(sum(x * x for x in a[:min_len])) or 1.0
    nb = math.sqrt(sum(x * x for x in b[:min_len])) or 1.0
    return dot / (na * nb)


def _rrf_score(rank: int, k: int = RRF_K) -> float:
    """Reciprocal Rank Fusion score."""
    return 1.0 / (k + rank)


def _bm25_scores(chunks: list[Chunk], query: str) -> list[float]:
    """Simple BM25 scoring without external dependencies.
    Uses term frequency and inverse document frequency."""
    query_terms = query.lower().split()
    if not query_terms or not chunks:
        return [0.0] * len(chunks)

    # Document frequencies
    N = len(chunks)
    df: dict[str, int] = {}
    doc_tokens: list[list[str]] = []

    for c in chunks:
        tokens = c.text.lower().split()
        doc_tokens.append(tokens)
        seen = set(tokens)
        for t in seen:
            df[t] = df.get(t, 0) + 1

    # Average document length
    avg_dl = sum(len(dt) for dt in doc_tokens) / max(N, 1)

    # BM25 parameters
    k1 = 1.5
    b = 0.75

    scores = []
    for tokens in doc_tokens:
        dl = len(tokens)
        score = 0.0
        tf_map: dict[str, int] = {}
        for t in tokens:
            tf_map[t] = tf_map.get(t, 0) + 1

        for qt in query_terms:
            if qt not in tf_map:
                continue
            tf = tf_map[qt]
            doc_freq = df.get(qt, 0)
            idf = math.log((N - doc_freq + 0.5) / (doc_freq + 0.5) + 1.0)
            tf_norm = (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / avg_dl))
            score += idf * tf_norm

        scores.append(score)

    return scores


async def rerank_chunks(
    query: str,
    query_vecs: list[list[float]],
    chunks: list[Chunk],
    *,
    top_k: int = TOP_K_RERANK,
    client: Optional[NIMClient] = None,
    use_nvidia_rerank: bool = True,
) -> list[Chunk]:
    """Hybrid reranking pipeline:
    1. (Optional) NVIDIA rerank API for fast logit-threshold filtering
    2. RRF fusion of dense cosine + BM25 sparse rankings

    Args:
        query: Original query string
        query_vecs: Embedding vectors for query variations
        chunks: Chunks with .embedding populated
        top_k: How many to return
        client: NIMClient for optional NVIDIA rerank API
        use_nvidia_rerank: Whether to try the NVIDIA rerank API first
    """
    if not chunks:
        return []

    # ----- Stage 1: Optional NVIDIA rerank API -----
    if use_nvidia_rerank and client:
        try:
            nvidia_results = await client.rerank(
                query,
                [c.text[:1000] for c in chunks],
            )
            if nvidia_results:
                # Apply logit threshold
                for r in nvidia_results:
                    idx = r["index"]
                    if 0 <= idx < len(chunks):
                        chunks[idx].score = r["logit"]

                # Filter by logit threshold
                mean_score = sum(c.score for c in chunks) / max(len(chunks), 1)
                cutoff = max(mean_score, RERANK_LOGIT_CUTOFF)
                filtered = [c for c in chunks if c.score >= cutoff]
                if filtered:
                    filtered.sort(key=lambda c: c.score, reverse=True)
                    return filtered[:top_k]
        except Exception:
            pass  # Fall through to RRF

    # ----- Stage 2: RRF (dense cosine + BM25) -----
    return _rrf_rerank(query, query_vecs, chunks, top_k)


def _rrf_rerank(
    query: str,
    query_vecs: list[list[float]],
    chunks: list[Chunk],
    top_k: int,
) -> list[Chunk]:
    """Pure RRF fusion — no API calls, pure computation."""
    if not chunks:
        return []

    # Dense ranking: max cosine similarity across all query variations
    for c in chunks:
        if c.embedding and query_vecs:
            c.score = max(cosine_sim(c.embedding, qv) for qv in query_vecs)
        else:
            c.score = 0.0

    dense_ranked = sorted(
        range(len(chunks)), key=lambda i: chunks[i].score, reverse=True
    )

    # Sparse ranking: BM25
    bm25 = _bm25_scores(chunks, query)
    sparse_ranked = sorted(
        range(len(chunks)), key=lambda i: bm25[i], reverse=True
    )

    # Build rank lookup
    dense_rank = {idx: rank for rank, idx in enumerate(dense_ranked)}
    sparse_rank = {idx: rank for rank, idx in enumerate(sparse_ranked)}

    # RRF fusion
    for i in range(len(chunks)):
        chunks[i].score = _rrf_score(dense_rank[i]) + _rrf_score(sparse_rank[i])

    chunks.sort(key=lambda c: c.score, reverse=True)
    return chunks[:top_k]
