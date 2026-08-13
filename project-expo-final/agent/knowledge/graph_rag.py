"""
Graph-enhanced retrieval — merges vector search with knowledge graph traversal.

From research: "Smart routing: use vector RAG for simple queries,
GraphRAG for relationship-heavy queries."

Pipeline:
  1. Vector search → top-k chunks (existing KB store)
  2. Extract entities from query
  3. Graph traverse from query entities → find connected facts
  4. Merge vector results + graph results
  5. Deduplicate by entity overlap

This adds ~160ms latency (150ms extraction + 5ms traversal + 5ms merge)
but enables multi-hop "connecting dots" that vector search can't.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from ..llm.client import NIMClient, get_client
from ..knowledge.kb_store import KBStore, get_kb_store
from ..knowledge.graph_store import (
    GraphStore, get_graph_store, extract_entities, Triple,
)
from ..blocks.semantic.types import Chunk, Learning

logger = logging.getLogger(__name__)


# ── Smart routing: should we use graph? ──

import json
import urllib.request
from ..config.settings import settings


def should_use_graph(query: str) -> bool:
    """Decide if a query benefits from graph traversal.
    Uses a fast synchronous LLM call to evaluate if the query is a relationship/multi-hop query.
    """
    if not settings.nim.api_keys:
        return False

    prompt = (
        "Analyze this user query to determine the required 'resolution' of the answer.\n"
        "Does this query require a high-resolution, multi-hop investigation (mapping surrounding historical context, "
        "hidden connections, causes, and related entities) to provide a truly superior answer? Or is it a low-resolution "
        "query that just needs a basic, single fact (e.g., 'what is 2+2', 'syntax for python print')?\n"
        "User query: " + query + "\n\n"
        "Output ONLY 'yes' (needs high-resolution/graph context) or 'no' (low-resolution fact)."
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
            return "yes" in ans
    except Exception:
        pass

    return False


async def graph_enhanced_retrieval(
    query: str,
    query_vec: list[float],
    *,
    kb: Optional[KBStore] = None,
    graph: Optional[GraphStore] = None,
    client: Optional[NIMClient] = None,
    top_k: int = 8,
    graph_hops: int = 2,
) -> list[Chunk]:
    """Merge vector search results with graph traversal results.

    1. Vector search (existing) → top-k chunks
    2. Extract query entities → graph traverse → connected facts
    3. Convert graph triples to Learning-compatible chunks
    4. Merge, deduplicate, sort by relevance
    """
    kb = kb or get_kb_store()
    graph = graph or get_graph_store()
    client = client or get_client()

    # ── 1. Vector search (standard) ──
    vector_chunks = kb.query(query_vec, top_k=top_k)

    # ── 2. Graph traversal ──
    if not should_use_graph(query) or graph.entity_count == 0:
        return vector_chunks

    # Extract entities from query
    query_entities = await _extract_query_entities(query, client)

    if not query_entities:
        return vector_chunks

    # Traverse graph from each query entity
    graph_triples: list[Triple] = []
    for entity in query_entities:
        triples = graph.query_entity(entity, hops=graph_hops)
        graph_triples.extend(triples)

    if not graph_triples:
        return vector_chunks

    # ── 3. Convert triples to chunks ──
    graph_chunks = _triples_to_chunks(graph_triples)

    # ── 4. Merge + deduplicate ──
    merged = _merge_and_dedup(vector_chunks, graph_chunks)

    logger.info(
        "Graph-enhanced retrieval: %d vector + %d graph = %d merged "
        "(from %d entities, %d triples)",
        len(vector_chunks), len(graph_chunks), len(merged),
        len(query_entities), len(graph_triples),
    )

    return merged


async def _extract_query_entities(query: str, client: NIMClient) -> list[str]:
    """Extract entity names from the query for graph lookup."""
    # Quick heuristic: capitalize words are likely entities
    words = query.split()
    entities = []

    for word in words:
        clean = re.sub(r"[^\w]", "", word)
        if clean and clean[0].isupper() and len(clean) > 2:
            entities.append(clean)

    # Also try extracting via LLM for better results
    try:
        messages = [
            {"role": "system", "content": (
                "Extract the key entity names from this query. "
                "Return a JSON array of strings. Just the entity names, nothing else."
            )},
            {"role": "user", "content": query},
        ]
        raw = await client.chat_worker(messages, temperature=0.0, max_tokens=100)

        import json
        text = raw.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            if text.endswith("```"):
                text = text[:-3]
        llm_entities = json.loads(text)
        if isinstance(llm_entities, list):
            entities.extend(str(e) for e in llm_entities)
    except Exception:
        pass  # Fall back to heuristic entities

    # Deduplicate
    seen = set()
    unique = []
    for e in entities:
        key = e.lower().strip()
        if key not in seen and len(key) > 1:
            seen.add(key)
            unique.append(e)

    return unique[:5]  # Cap at 5 entities


def _triples_to_chunks(triples: list[Triple]) -> list[Chunk]:
    """Convert graph triples to Chunk objects for merging."""
    chunks = []
    seen = set()

    for t in triples:
        key = f"{t.subject}|{t.predicate}|{t.object}"
        if key in seen:
            continue
        seen.add(key)

        text = f"{t.subject} {t.predicate} {t.object}"
        chunks.append(Chunk(
            text=text,
            source_url=t.source_url or "knowledge_graph",
            title=f"{t.subject} → {t.predicate} → {t.object}",
            score=t.confidence * 0.8,  # Slightly lower than vector results
        ))

    return chunks


def _merge_and_dedup(
    vector_chunks: list[Chunk],
    graph_chunks: list[Chunk],
) -> list[Chunk]:
    """Merge vector + graph results, deduplicating by content overlap."""
    merged = list(vector_chunks)
    vector_texts = {c.text.lower()[:100] for c in vector_chunks}

    for gc in graph_chunks:
        # Skip if very similar to existing vector result
        gc_text = gc.text.lower()[:100]
        is_dup = any(
            _text_overlap(gc_text, vt) > 0.7
            for vt in vector_texts
        )
        if not is_dup:
            merged.append(gc)

    # Sort by score
    merged.sort(key=lambda c: c.score, reverse=True)
    return merged


def _text_overlap(a: str, b: str) -> float:
    """Quick text overlap ratio (Jaccard similarity of words)."""
    words_a = set(a.split())
    words_b = set(b.split())
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union)
