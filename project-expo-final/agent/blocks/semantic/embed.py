"""
Embedding module — wraps NIMClient.embed() with the two-layer cache.

The cache check happens HERE, not in the client — the client is a raw
API wrapper, this module is the "smart" layer that avoids redundant calls.
"""

from __future__ import annotations

from typing import Optional

from ...llm.client import NIMClient, get_client
from ...cache.embed_cache import EmbedCache, get_embed_cache
from ...config.budgets import EMBED_DIM
from .types import Chunk


async def embed_texts(
    texts: list[str],
    *,
    input_type: str = "passage",
    dim: int = EMBED_DIM,
    client: Optional[NIMClient] = None,
    cache: Optional[EmbedCache] = None,
) -> list[list[float]]:
    """Embed texts with two-layer caching. Only calls the API for uncached texts."""
    client = client or get_client()
    cache = cache or get_embed_cache()

    if not texts:
        return []

    # Check cache for each text
    results, uncached_indices = cache.get_batch(texts)

    if uncached_indices:
        # Only embed what's not cached
        uncached_texts = [texts[i] for i in uncached_indices]
        new_vecs = await client.embed(uncached_texts, input_type=input_type, dim=dim)

        # Store in cache and fill results
        for idx, vec in zip(uncached_indices, new_vecs):
            results[idx] = vec
            cache.set(texts[idx], vec)

    # At this point all results should be populated (type narrowing)
    return [v for v in results if v is not None]


async def embed_chunks(
    chunks: list[Chunk],
    *,
    client: Optional[NIMClient] = None,
    cache: Optional[EmbedCache] = None,
) -> list[Chunk]:
    """Embed a list of Chunk objects in place. Returns the same list."""
    if not chunks:
        return chunks

    texts = [c.text for c in chunks]
    vecs = await embed_texts(texts, input_type="passage", client=client, cache=cache)

    for chunk, vec in zip(chunks, vecs):
        chunk.embedding = vec

    return chunks


async def embed_query(
    query: str,
    *,
    client: Optional[NIMClient] = None,
) -> list[float]:
    """Embed a single query string. Uses input_type='query' for asymmetric models."""
    client = client or get_client()
    vecs = await client.embed([query], input_type="query")
    return vecs[0] if vecs else []
