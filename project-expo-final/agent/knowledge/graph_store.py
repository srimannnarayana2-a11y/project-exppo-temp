"""
Knowledge graph store — entity-relationship graph for connecting dots.

From chats L155:
  "Shannon entropy, surprisal value, graph centrality (degree+betweenness)
   for selecting which nodes to expand. Knowledge graph connecting dots:
   entity extraction, source→relationship→target."

From chats L320 (DOB/age principle):
  "Prefer the fundamental fact over the derivable one when text states both."

Architecture:
  - Simple in-memory adjacency list (upgradeable to Neo4j later)
  - Entity extraction via LLM structured output
  - N-hop traversal from query entities
  - Stores triples: (entity, relationship, entity, source_url)
  - Persistent save/load to JSON
"""

from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from ..llm.client import NIMClient, get_client
from ..config.settings import settings

logger = logging.getLogger(__name__)


@dataclass
class Triple:
    """One entity-relationship-entity triple."""
    subject: str           # e.g. "Adolf Hitler"
    predicate: str         # e.g. "married"
    object: str            # e.g. "Eva Braun"
    source_url: str = ""   # provenance
    confidence: float = 1.0


@dataclass
class GraphNode:
    """One entity in the graph."""
    name: str
    triples: list[Triple] = field(default_factory=list)
    mention_count: int = 0  # how many times this entity appears


class GraphStore:
    """In-memory entity-relationship graph.

    Simple adjacency list. O(1) entity lookup, O(edges) traversal.
    Upgradeable to Neo4j/ArangoDB later without changing interface.
    """

    def __init__(self, persist_dir: str = ""):
        self._adjacency: dict[str, list[Triple]] = defaultdict(list)
        self._entity_counts: dict[str, int] = defaultdict(int)
        self._persist_dir = persist_dir or getattr(settings, 'kb_dir', '.kb_data')

    @property
    def entity_count(self) -> int:
        return len(self._adjacency)

    @property
    def triple_count(self) -> int:
        return sum(len(triples) for triples in self._adjacency.values())

    def add_triples(self, triples: list[Triple]):
        """Add triples to the graph. Deduplicates by (subject, predicate, object)."""
        for t in triples:
            key = t.subject.lower().strip()
            # Dedup check
            existing = self._adjacency[key]
            is_dup = any(
                e.predicate.lower() == t.predicate.lower() and
                e.object.lower() == t.object.lower()
                for e in existing
            )
            if not is_dup:
                self._adjacency[key].append(t)
                self._entity_counts[key] += 1
                self._entity_counts[t.object.lower().strip()] += 1

    def query_entity(self, entity: str, hops: int = 2) -> list[Triple]:
        """Traverse N hops from an entity, collecting all connected triples.

        hop=1: direct connections
        hop=2: connections of connections (most useful for "connecting dots")
        """
        entity_key = entity.lower().strip()
        visited: set[str] = set()
        result: list[Triple] = []
        queue = [(entity_key, 0)]

        while queue:
            current, depth = queue.pop(0)
            if current in visited or depth > hops:
                continue
            visited.add(current)

            triples = self._adjacency.get(current, [])
            result.extend(triples)

            if depth < hops:
                for t in triples:
                    obj_key = t.object.lower().strip()
                    if obj_key not in visited:
                        queue.append((obj_key, depth + 1))
                    subj_key = t.subject.lower().strip()
                    if subj_key not in visited and subj_key != current:
                        queue.append((subj_key, depth + 1))

        return result

    def get_central_entities(self, top_k: int = 10) -> list[tuple[str, int]]:
        """Get the most connected entities (degree centrality)."""
        return sorted(
            self._entity_counts.items(),
            key=lambda x: x[1],
            reverse=True,
        )[:top_k]

    def save(self):
        """Persist to JSON."""
        os.makedirs(self._persist_dir, exist_ok=True)
        path = os.path.join(self._persist_dir, "graph.json")

        data = []
        for key, triples in self._adjacency.items():
            for t in triples:
                data.append({
                    "s": t.subject, "p": t.predicate, "o": t.object,
                    "src": t.source_url, "c": t.confidence,
                })

        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        logger.info("Graph saved: %d triples to %s", len(data), path)

    def load(self) -> bool:
        """Load from JSON."""
        path = os.path.join(self._persist_dir, "graph.json")
        if not os.path.exists(path):
            return False

        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)

            for entry in data:
                t = Triple(
                    subject=entry["s"], predicate=entry["p"], object=entry["o"],
                    source_url=entry.get("src", ""), confidence=entry.get("c", 1.0),
                )
                key = t.subject.lower().strip()
                self._adjacency[key].append(t)
                self._entity_counts[key] += 1
                self._entity_counts[t.object.lower().strip()] += 1

            logger.info("Graph loaded: %d triples from %s", len(data), path)
            return True
        except Exception as e:
            logger.warning("Graph load failed: %s", e)
            return False

    def clear(self):
        self._adjacency.clear()
        self._entity_counts.clear()


# ── Entity extraction via LLM ──

_EXTRACT_PROMPT = (
    "Extract entity-relationship triples from this text. Return a JSON array "
    "where each element has: s (subject), p (predicate/relationship), o (object).\n\n"
    "Rules:\n"
    "1. FUNDAMENTAL FACTS ONLY: prefer DOB over age, prefer cause over effect "
    "(if you know DOB, age is derivable — don't extract it separately).\n"
    "2. SPECIFIC entities: 'Adolf Hitler' not 'he'. 'Python 3.12' not 'the language'.\n"
    "3. MEANINGFUL relationships: 'married', 'founded', 'defeated_by', 'released_in' "
    "— not 'is related to'.\n"
    "4. MAX 15 triples per text block. Focus on the most important.\n"
    "5. Return ONLY valid JSON array, no markdown fences.\n\n"
    "Example: [{\"s\": \"Albert Einstein\", \"p\": \"born_in\", \"o\": \"Ulm, Germany\"}, "
    "{\"s\": \"Albert Einstein\", \"p\": \"published\", \"o\": \"Theory of Relativity\"}]"
)


async def extract_entities(
    text: str,
    source_url: str = "",
    *,
    client: Optional[NIMClient] = None,
) -> list[Triple]:
    """Extract entity-relationship triples from text using LLM.

    Uses the same NIM model (free keys), ~150ms latency.
    """
    client = client or get_client()

    # Truncate text to save tokens
    truncated = text[:3000] if len(text) > 3000 else text

    messages = [
        {"role": "system", "content": _EXTRACT_PROMPT},
        {"role": "user", "content": truncated},
    ]

    try:
        raw = await client.chat_worker(messages, temperature=0.1, max_tokens=800)

        # Parse JSON
        text_clean = raw.strip()
        if text_clean.startswith("```"):
            text_clean = text_clean.split("\n", 1)[1]
            if text_clean.endswith("```"):
                text_clean = text_clean[:-3]

        data = json.loads(text_clean)
        if not isinstance(data, list):
            return []

        triples = []
        for entry in data[:15]:  # Cap at 15
            if all(k in entry for k in ("s", "p", "o")):
                triples.append(Triple(
                    subject=str(entry["s"]),
                    predicate=str(entry["p"]),
                    object=str(entry["o"]),
                    source_url=source_url,
                ))

        return triples
    except Exception as e:
        logger.warning("Entity extraction failed: %s", e)
        return []


# ── Module singleton ──

_store: Optional[GraphStore] = None


def get_graph_store() -> GraphStore:
    global _store
    if _store is None:
        _store = GraphStore()
        _store.load()
    return _store
