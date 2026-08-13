"""
github_research_tool.py

Adaptive Speculative RAG - GitHub Research Tool (v2 — Optimized)
-----------------------------------------------------------------
A single callable "tool" an LLM agent can invoke via function-calling.
Given a query + model-decided parameters, it:

  1. Expands the query via LLM → GitHub-optimized search terms, topics, exclusions
  2. Discovers repos via TWO-PRONG search: Code Search API + Topic-qualified Repo Search
  3. Filters by metadata (stars range, language, recency, license)
  4. Per-repo parallel pipeline: tree → LLM file select → fetch → clean →
     AST extract → LLM hybrid refine+context (ONE call per file)
  5. Deduplicates chunks (SHA-256 exact + near-duplicate detection)
  6. Streams chunks to embedding as each repo finishes (NVIDIA Nemotron)
  7. Multi-query expansion (NOT HyDE) → embeds query variations
  8. Reranks by RRF (Reciprocal Rank Fusion) of dense cosine + BM25
  9. Final LLM judge → path dedup → return top-N chunks

Key design changes from v1:
  - HyDE replaced with Multi-Query Expansion (HyDE is counterproductive for code)
  - quick_relevance_filter + contextualize_chunk collapsed into ONE LLM call per file
  - Linear weighted fusion replaced with RRF (more robust without tuning)
  - Chunk deduplication added (fixes CLoUDShell.py ×6 bug)
  - Streaming embedder (don't wait for slowest repo)
  - Multi-stage search (fixes irrelevant repo discovery)

References:
  - Speculative RAG (Wang et al., 2024) - arXiv:2407.08223
  - Contextual Retrieval - https://www.anthropic.com/engineering/contextual-retrieval
  - Reciprocal Rank Fusion (Cormack et al., 2009)

Env vars required:
  GITHUB_TOKEN       - GitHub personal access token (raises rate limit)
  NVIDIA_API_KEY     - from build.nvidia.com, used for Nemotron embed + LLM
"""

import os
import re
import ast
import math
import time
import json
import sqlite3
import hashlib
import asyncio
import aiohttp
import numpy as np
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Optional

from openai import AsyncOpenAI, APIStatusError, APIConnectionError
from rank_bm25 import BM25Okapi


# ============================================================================
# 0. TOOL SCHEMA — what you hand to the LLM for function-calling
# ============================================================================

TOOL_SCHEMA = {
    "name": "code_retriever_tool",
    "description": (
        "Deep code retrieval tool. Searches GitHub for repositories relevant to a query, "
        "extracts structurally relevant code chunks using AST parsing + LLM refinement, "
        "and returns them with confidence scoring. Includes adaptive gating (skips retrieval "
        "for parametric queries the LLM already knows) and deep proposition extraction "
        "(multi-hop connected-dots decomposition for non-obvious code discovery). "
        "Returns retrieval_signal: HIGH/MEDIUM/LOW/EMPTY/SKIP so the agent knows "
        "whether to trust results, try semantic retrieval, or ask a clarifying question."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The research query or task the code should help with.",
            },
            "keywords": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Key technical terms to bias the search query.",
            },
            "language": {
                "type": "string",
                "description": "Primary language filter, e.g. 'python'. Optional.",
            },
            "deep_search": {
                "type": "boolean",
                "description": (
                    "Enable deep proposition extraction for multi-hop code discovery. "
                    "When True, decomposes the query into specific technical propositions "
                    "and searches for each independently. Finds non-obvious implementations "
                    "but adds ~1s latency. Default True."
                ),
            },
            "min_stars": {
                "type": "integer",
                "description": "Minimum stars. Default 5.",
            },
            "max_stars": {
                "type": "integer",
                "description": "Maximum stars. Default 2000.",
            },
            "max_repos": {
                "type": "integer",
                "description": "Max repos after filtering. Default 6.",
            },
            "top_chunks": {
                "type": "integer",
                "description": "Final chunks to return. Default 8.",
            },
        },
        "required": ["query"],
    },
}


# ============================================================================
# 1. CONFIG
#
#    Multi-key architecture: with 2 API keys from SEPARATE NVIDIA accounts,
#    each key has its own independent rate limit budget. This means we can
#    run 2× the concurrent LLM calls without hitting 429s — each key gets
#    its own semaphore (PER_KEY_CONCURRENCY slots), and calls are routed
#    round-robin across keys so both budgets are consumed evenly.
#
#    Env var patterns supported:
#      NVIDIA_API_KEY_1 / NVIDIA_API_KEY_2    (preferred, explicit)
#      NVIDIA_LLM_KEY_1 .. NVIDIA_LLM_KEY_4  (legacy, still works)
#      NVIDIA_API_KEY                          (single key fallback)
# ============================================================================

def get_github_token() -> str:
    return os.environ.get("GITHUB_TOKEN", "")


def get_all_api_keys() -> list:
    """Collect all available NVIDIA API keys. Tries CODE_NIM_KEY_1/2 first, 
    then NVIDIA_API_KEY_1/2, then legacy keys. Returns deduplicated list."""
    keys = []
    # Dedicated Code Pool
    for i in range(1, 5):
        k = os.environ.get(f"CODE_NIM_KEY_{i}")
        if k:
            keys.append(k)
    # Generic Pool (preferred)
    if not keys:
        for i in range(1, 5):
            k = os.environ.get(f"NVIDIA_API_KEY_{i}")
            if k:
                keys.append(k)
    # Legacy pattern
    if not keys:
        for i in range(1, 5):
            k = os.environ.get(f"NVIDIA_LLM_KEY_{i}")
            if k:
                keys.append(k)
    # Single key fallback
    if not keys:
        k = os.environ.get("NVIDIA_API_KEY", "")
        if k:
            keys.append(k)
    # Deduplicate while preserving order
    seen = set()
    unique = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            unique.append(k)
    return unique


def get_embed_keys() -> list:
    """Embed keys — uses all available API keys for round-robin embedding
    too, since each key has its own rate limit budget."""
    # Prefer dedicated embed key if set
    dedicated = os.environ.get("NVIDIA_EMBED_API_KEY")
    if dedicated:
        return [dedicated]
    return get_all_api_keys()


NVIDIA_EMBED_URL = "https://integrate.api.nvidia.com/v1/embeddings"
NVIDIA_EMBED_MODEL = "nvidia/nemotron-3-embed-1b"
EMBED_DIM = 512  # Matryoshka slice — cheap first pass

NVIDIA_CHAT_MODEL = "meta/llama-3.3-70b-instruct"          # reasoning: judge, search expansion
NVIDIA_CONTEXT_MODEL = "meta/llama-3.1-8b-instruct"         # speed: file select, chunk refine

# With 2 keys from separate accounts: 5 concurrent calls PER key = 10 total.
# With 1 key: 5 concurrent calls total (safe default, won't 429).
PER_KEY_CONCURRENCY = 5
EMBED_BATCH_SIZE = 16

# Timeouts — prevent one stalled API from blocking the entire pipeline
GITHUB_TIMEOUT = aiohttp.ClientTimeout(total=15)
LLM_TIMEOUT = 30
EMBED_TIMEOUT = 20

MIN_FILE_CHARS = 200  # skip near-empty files entirely


# ============================================================================
# 1a. CACHING INFRASTRUCTURE — multi-layer caches for latency reduction
# ============================================================================

# Repo tree cache (TTL=1 hour): GitHub trees rarely change
_TREE_CACHE = {}  # key: "owner/repo:branch" → value: (tree_list, timestamp)
TREE_CACHE_TTL = 3600  # 1 hour

# Embedding cache (SHA-256 content hash): same content → same vector
_EMBED_CACHE = {}  # key: sha256(content) → value: normalized embedding vector

# LLM response cache (prompt hash): deterministic LLM calls
_LLM_CACHE = {}  # key: sha256(prompt|model) → value: response text
LLM_CACHE_MAX = 500  # max entries before LRU eviction


def _cache_key_llm(prompt: str, model: str, system: str = "") -> str:
    """Generate cache key for deterministic LLM calls."""
    raw = f"{prompt}|{model}|{system or ''}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _cache_key_embed(text: str) -> str:
    """Generate cache key for embedding cache."""
    return hashlib.sha256(text.encode()).hexdigest()


def _evict_lru_cache(cache: dict, max_size: int):
    """Simple size-based eviction — remove oldest entries."""
    if len(cache) > max_size:
        excess = len(cache) - max_size
        for key in list(cache.keys())[:excess]:
            del cache[key]


# SQLite Persistent Cache for cross-session / cross-cell acceleration
_DB_PATH = os.environ.get("RAG_CACHE_DB_PATH", ".rag_cache.db")


def _init_sqlite_db():
    try:
        conn = sqlite3.connect(_DB_PATH, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tree_cache (
                    key TEXT PRIMARY KEY,
                    data TEXT,
                    timestamp REAL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS embed_cache (
                    key TEXT PRIMARY KEY,
                    vec_json TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS llm_cache (
                    key TEXT PRIMARY KEY,
                    response TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS result_cache (
                    key TEXT PRIMARY KEY,
                    result_json TEXT,
                    timestamp REAL
                )
            """)
        conn.close()
    except Exception:
        pass


_init_sqlite_db()


def _db_get_tree(key: str, ttl: float = 3600) -> Optional[list]:
    try:
        conn = sqlite3.connect(_DB_PATH, timeout=2.0)
        cur = conn.cursor()
        cur.execute("SELECT data, timestamp FROM tree_cache WHERE key = ?", (key,))
        row = cur.fetchone()
        conn.close()
        if row:
            data_str, ts = row
            if time.time() - ts < ttl:
                return json.loads(data_str)
    except Exception:
        pass
    return None


def _db_set_tree(key: str, tree: list):
    """Cache repo tree only if non-empty (never cache network timeouts/failures)."""
    if not tree:
        return
    try:
        conn = sqlite3.connect(_DB_PATH, timeout=2.0)
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO tree_cache (key, data, timestamp) VALUES (?, ?, ?)",
                (key, json.dumps(tree), time.time())
            )
        conn.close()
    except Exception:
        pass


def _db_get_embed(key: str) -> Optional[list]:
    try:
        conn = sqlite3.connect(_DB_PATH, timeout=2.0)
        cur = conn.cursor()
        cur.execute("SELECT vec_json FROM embed_cache WHERE key = ?", (key,))
        row = cur.fetchone()
        conn.close()
        if row:
            return json.loads(row[0])
    except Exception:
        pass
    return None


def _db_set_embed(key: str, vec: list):
    """Cache embedding vector only if valid non-empty list."""
    if not vec:
        return
    try:
        conn = sqlite3.connect(_DB_PATH, timeout=2.0)
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO embed_cache (key, vec_json) VALUES (?, ?)",
                (key, json.dumps(vec))
            )
        conn.close()
    except Exception:
        pass


def _db_get_llm(key: str) -> Optional[str]:
    try:
        conn = sqlite3.connect(_DB_PATH, timeout=2.0)
        cur = conn.cursor()
        cur.execute("SELECT response FROM llm_cache WHERE key = ?", (key,))
        row = cur.fetchone()
        conn.close()
        if row:
            return row[0]
    except Exception:
        pass
    return None


def _db_set_llm(key: str, resp: str):
    """Cache LLM response only if non-empty and non-error (never cache API failures)."""
    if not resp or not resp.strip() or "error" in resp.lower() or "failed" in resp.lower():
        return
    try:
        conn = sqlite3.connect(_DB_PATH, timeout=2.0)
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO llm_cache (key, response) VALUES (?, ?)",
                (key, resp)
            )
        conn.close()
    except Exception:
        pass


def _db_get_result(key: str, ttl: float = 900) -> Optional[dict]:
    try:
        conn = sqlite3.connect(_DB_PATH, timeout=2.0)
        cur = conn.cursor()
        cur.execute("SELECT result_json, timestamp FROM result_cache WHERE key = ?", (key,))
        row = cur.fetchone()
        conn.close()
        if row:
            res_str, ts = row
            if time.time() - ts < ttl:
                return json.loads(res_str)
    except Exception:
        pass
    return None


def _db_set_result(key: str, result: dict):
    """Cache query result only if successful with retrieved chunks (never cache empty/failed retrieval)."""
    if not result or not result.get("chunks") or result.get("retrieval_signal") in ("EMPTY", "SKIP"):
        return
    try:
        conn = sqlite3.connect(_DB_PATH, timeout=2.0)
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO result_cache (key, result_json, timestamp) VALUES (?, ?, ?)",
                (key, json.dumps(result), time.time())
            )
        conn.close()
    except Exception:
        pass


# ============================================================================
# 1b. DATA CLASSES
# ============================================================================

@dataclass
class CodeChunk:
    repo: str
    path: str
    content: str
    stars: int
    embedding: Optional[list] = field(default=None, repr=False)
    score: float = 0.0


@dataclass
class ASTNode:
    """Structured AST extraction result with metadata for LLM refinement."""
    index: int
    node_type: str          # "function", "class", "async_function"
    name: str               # "connect_shell", "PayloadEncoder"
    source: str             # full source code
    preceding_comments: str # comment block above the node
    line_range: tuple       # (start_line, end_line)
    line_count: int


@dataclass
class SearchProposition:
    """A single deep technical proposition extracted from a user query.
    Each proposition becomes a separate code search query."""
    proposition: str    # e.g. "reverse TCP shell using pty.spawn and os.dup2"
    code_terms: list    # e.g. ["pty.spawn", "os.dup2", "socket.connect"]


@dataclass
class SearchExpansion:
    """LLM-generated search expansion for GitHub queries."""
    code_terms: list        # e.g. ["subprocess.Popen", "socket.connect"]
    topics: list            # e.g. ["pentesting", "linux", "bash"]
    exclude_topics: list    # e.g. ["awesome", "tutorial"]
    extra_extensions: list  # e.g. [".sh", ".bash"] if shell-related


# ============================================================================
# 2. ASYNC INFRASTRUCTURE — per-key semaphores, backoff, LLM client pool
#
#    With 2 API keys from separate accounts, each key has its own rate limit.
#    Instead of one shared semaphore that bottlenecks everything:
#      - Each key gets its own semaphore (PER_KEY_CONCURRENCY slots)
#      - Calls are routed round-robin: call N goes to key N % num_keys
#      - Total concurrency = num_keys × PER_KEY_CONCURRENCY
#      - With 2 keys: 2 × 5 = 10 concurrent LLM calls
#      - With 1 key: 1 × 5 = 5 concurrent (safe fallback)
#
#    Deadlock safety (same rules as before):
#      - real_llm_call() is the ONLY function that acquires a key semaphore
#      - No nesting: higher-level callers never acquire semaphores themselves
#      - No mixing: LLM semaphores and GitHub semaphore are never held together
#      - Each call acquires exactly ONE semaphore from ONE key's pool
# ============================================================================

# Per-key semaphores + cached clients, built lazily on first call
_key_slots: list = []    # [(semaphore, AsyncOpenAI client), ...] per key
_key_slots_init = False


def _ensure_key_slots():
    """Lazy init: build per-key semaphore+client pairs from env vars.
    Called once on first LLM call, not at module import (env vars might
    not be set yet at import time — the exact bug the v1 comments warned about)."""
    global _key_slots, _key_slots_init
    if _key_slots_init:
        return
    keys = get_all_api_keys()
    _key_slots = [
        (
            asyncio.Semaphore(PER_KEY_CONCURRENCY),
            AsyncOpenAI(base_url="https://integrate.api.nvidia.com/v1", api_key=k),
        )
        for k in keys
    ]
    _key_slots_init = True


# Separate semaphore for GitHub API calls (separate rate limit budget).
_github_semaphore = asyncio.Semaphore(8)

# Atomic counter for round-robin key selection
_llm_call_counter = 0


def _pick_key_slot() -> tuple:
    """Round-robin across key slots. Returns (semaphore, client).
    Each call gets a different key, distributing load evenly across
    both accounts' rate limit budgets."""
    global _llm_call_counter
    _ensure_key_slots()
    if not _key_slots:
        raise RuntimeError(
            "No NVIDIA API keys found. Set NVIDIA_API_KEY_1 / NVIDIA_API_KEY_2 "
            "or NVIDIA_API_KEY environment variable."
        )
    idx = _llm_call_counter % len(_key_slots)
    _llm_call_counter += 1
    return _key_slots[idx]


async def call_with_backoff(fn, *args, max_retries: int = 5, **kwargs):
    """Retry with exponential backoff on rate-limit and server errors."""
    for attempt in range(max_retries):
        try:
            return await fn(*args, **kwargs)
        except Exception as e:
            msg = str(e)
            is_retryable = any(code in msg for code in ("429", "500", "502", "503", "504", "Too Many Requests"))
            if is_retryable and attempt < max_retries - 1:
                wait = 2 ** attempt
                await asyncio.sleep(wait)
            else:
                raise


async def real_llm_call(
    prompt: str,
    max_tokens: int = 300,
    model: str = NVIDIA_CHAT_MODEL,
    system: Optional[str] = None,
    timeout: Optional[float] = None,
    use_cache: bool = False,
) -> str:
    """LLM call with per-key semaphore, round-robin routing, backoff, timeout."""
    if use_cache:
        ckey = _cache_key_llm(prompt, model, system or "")
        if ckey in _LLM_CACHE:
            return _LLM_CACHE[ckey]

    sem, client = _pick_key_slot()
    effective_timeout = timeout if timeout is not None else LLM_TIMEOUT

    async with sem:
        async def _call():
            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": prompt})
            try:
                resp = await asyncio.wait_for(
                    client.chat.completions.create(
                        model=model,
                        messages=messages,
                        max_tokens=max_tokens,
                        temperature=0.3,
                    ),
                    timeout=effective_timeout,
                )
                return resp.choices[0].message.content
            except (APIStatusError, APIConnectionError) as e:
                raise RuntimeError(f"NVIDIA chat API error: {e}") from e

        res = await call_with_backoff(_call)
        if use_cache:
            ckey = _cache_key_llm(prompt, model, system or "")
            _LLM_CACHE[ckey] = res
            _evict_lru_cache(_LLM_CACHE, LLM_CACHE_MAX)
        return res


# ============================================================================
# 3. GITHUB SEARCH — Multi-Stage Discovery
# ============================================================================

async def expand_search_query(
    llm_call_fn, query: str, keywords: list, language: str
) -> SearchExpansion:
    """Universal LLM query expansion — converts ANY user query into optimal GitHub Code & Topic search primitives."""
    system_prompt = (
        "You are an expert Code Retrieval Architect. Your task is to analyze ANY software task "
        "and extract precise technical search primitives for the GitHub Code & Repository Search APIs.\n\n"
        "Apply these universal principles:\n"
        "1. CODE_TERMS: Identify 3-5 core structural implementation primitives (exact API method names, "
        "low-level library imports, or internal syscalls) that ONLY an actual functional implementation of this "
        "task would write.\n"
        "CRITICAL RULE: CODE_TERMS MUST NOT be broad high-level English words from the query (like 'linux', 'shell', 'hacking', 'python', 'script', 'tool'). "
        "They MUST be exact API method names, library function signatures, or code primitives (e.g. `pty.spawn`, `os.dup2`, `socket.connect`, `mmap`, `ctypes`).\n"
        "2. TOPICS: Identify 3-5 specific GitHub repository topics that domain experts tag their repos with.\n"
        "3. EXCLUDE: Identify 2-3 noise topics common to this topic area (e.g., 'awesome', 'tutorial', 'list', 'course', 'cheatsheet', 'template').\n"
        "4. EXTENSIONS: Identify non-standard file extensions critical for this domain (or 'none')."
    )
    prompt = (
        f"Query: {query}\n"
        f"Keywords: {', '.join(keywords) if keywords else 'none'}\n"
        f"Language: {language or 'any'}\n\n"
        "Generate GitHub search optimization data. Output ONLY in this exact format:\n"
        "CODE_TERMS: term1, term2, term3\n"
        "TOPICS: topic1, topic2, topic3\n"
        "EXCLUDE: topic1, topic2\n"
        "EXTENSIONS: .ext1, .ext2"
    )
    try:
        result = await llm_call_fn(
            prompt, max_tokens=140, model=NVIDIA_CHAT_MODEL,
            system=system_prompt,
        )
        expansion = SearchExpansion([], [], [], [])
        for line in result.strip().splitlines():
            line = line.strip()
            if line.upper().startswith("CODE_TERMS:"):
                expansion.code_terms = [t.strip() for t in line.split(":", 1)[1].split(",") if t.strip()]
            elif line.upper().startswith("TOPICS:"):
                expansion.topics = [t.strip() for t in line.split(":", 1)[1].split(",") if t.strip()]
            elif line.upper().startswith("EXCLUDE:"):
                expansion.exclude_topics = [t.strip() for t in line.split(":", 1)[1].split(",") if t.strip()]
            elif line.upper().startswith("EXTENSIONS:"):
                exts = line.split(":", 1)[1].strip()
                if exts.lower() != "none":
                    expansion.extra_extensions = [e.strip() for e in exts.split(",") if e.strip()]
        return expansion
    except Exception:
        return SearchExpansion(
            code_terms=keywords[:5] if keywords else query.split()[:5],
            topics=[], exclude_topics=[], extra_extensions=[],
        )


async def gate_code_retrieval(llm_call_fn, query: str, keywords: list) -> dict:
    """Lightweight classifier (~0.3s). Decides if query needs code retrieval.
    Uses regex fast-path for 90% of queries, LLM fallback for ambiguous cases."""
    # ── Regex fast-path (0 ms) ──────────────────────────────────────────
    q_lower = query.lower()
    PARAMETRIC_PATTERNS = re.compile(
        r"^(what is|explain|define|how does|why does|difference between|"
        r"when to use|compare|pros and cons|best practices for|describe|summarize)\b",
        re.IGNORECASE
    )
    CODE_PATTERNS = re.compile(
        r"(implement|exploit|script|hack|reverse.?shell|payload|brute.?force|"
        r"injection|scan|scrape|crawl|automate|bot|socket|subprocess|os\.system|"
        r"malware|keylogger|privilege.?escalation|buffer.?overflow|fuzzer|decompil)",
        re.IGNORECASE
    )
    if PARAMETRIC_PATTERNS.match(query):
        return {
            "should_retrieve": False,
            "reason": "Regex: parametric knowledge query",
            "suggested_mode": "PARAMETRIC",
        }
    if CODE_PATTERNS.search(query) or any(kw.lower() in q_lower for kw in keywords):
        return {
            "should_retrieve": True,
            "reason": "Regex: code implementation query",
            "suggested_mode": "CODE",
        }

    # ── LLM fallback for ambiguous queries ──────────────────────────────
    system_prompt = (
        "You are a Retrieval Necessity Classifier. Given a user query, determine whether "
        "it requires external code retrieval from GitHub, or can be answered from LLM parametric knowledge.\n\n"
        "Classification rules:\n"
        "- PARAMETRIC: Standard algorithms, language syntax, common patterns, well-documented APIs, "
        "math/logic, basic data structures. The LLM already knows these — no retrieval needed.\n"
        "- CODE: Needs real-world implementation examples, niche libraries, domain-specific tools, "
        "exploit techniques, custom frameworks, or code that doesn't exist in standard docs.\n"
        "- SEMANTIC: Needs conceptual explanation, documentation, research papers, or articles "
        "rather than raw code.\n"
        "- HYBRID: Needs both code examples AND conceptual grounding.\n\n"
        "Output ONLY in this format:\n"
        "MODE: PARAMETRIC|CODE|SEMANTIC|HYBRID\n"
        "REASON: <one sentence>"
    )
    prompt = f"Query: {query}\nKeywords: {', '.join(keywords) if keywords else 'none'}"

    try:
        result = await llm_call_fn(
            prompt, max_tokens=60, model=NVIDIA_CONTEXT_MODEL,
            system=system_prompt, timeout=2.0,
        )
        mode = "CODE"
        reason = "default"
        for line in result.strip().splitlines():
            line = line.strip()
            if line.upper().startswith("MODE:"):
                mode = line.split(":", 1)[1].strip().upper()
                if mode not in ("PARAMETRIC", "CODE", "SEMANTIC", "HYBRID"):
                    mode = "CODE"
            elif line.upper().startswith("REASON:"):
                reason = line.split(":", 1)[1].strip()

        should_retrieve = mode in ("CODE", "HYBRID")
        return {
            "should_retrieve": should_retrieve,
            "reason": reason,
            "suggested_mode": mode,
        }
    except Exception:
        # On failure, default to retrieval (safer than skipping)
        return {"should_retrieve": True, "reason": "gating classifier failed", "suggested_mode": "CODE"}


async def extract_deep_propositions(
    llm_call_fn, query: str, keywords: Optional[list] = None
) -> list:
    """Extract 3-5 deep technical search propositions from the query.
    Deconstructs high-level queries into distinct implementation primitives using connected-dots deduction."""
    keywords = keywords or []
    system_prompt = (
        "You are an Investigative Code Architect & Technical Deduction Engine. Your task is to perform "
        "'Dot-Connecting' technical deduction on ANY user query (AI/ML, systems, security, databases, web, networking, compilers):\n\n"
        "Connect the technical implementation dots and generate 3-5 distinct, non-overlapping propositions:\n"
        "1. DEDUCE HIDDEN DEPENDENCIES: What underlying syscalls, internal library APIs, protocol handshakes, "
        "memory operations, or data structure hooks must a REAL working implementation inevitably use?\n"
        "2. EXPLICIT CODE PRIMITIVES: You MUST output exact technical code symbols, API signatures, library imports, or syscall names "
        "(e.g., `ptrace`, `LD_PRELOAD`, `os.dup2`, `pty.spawn`, `socket.connect`, `mmap`, `ctypes`, `subprocess`, `termios`, `torch.cuda`, `epoll`).\n"
        "3. AVOID GENERIC HUMAN WORDS: Completely reject broad human words from the query (like 'linux', 'shell', 'hacking', 'python', 'script', 'tool', 'code').\n\n"
        "Output format (one per line, nothing else):\n"
        "P1: <technical aspect summary> | TERMS: symbol1, symbol2, symbol3\n"
        "P2: <technical aspect summary> | TERMS: symbol1, symbol2, symbol3"
    )
    prompt = (
        f"Query: {query}\n"
        f"Keywords: {', '.join(keywords) if keywords else 'none'}\n\n"
        "Generate 3-5 sharp technical code propositions with exact API/symbol terms. Output ONLY the P1/P2/P3 lines:"
    )
    try:
        result = await llm_call_fn(
            prompt, max_tokens=220, model=NVIDIA_CHAT_MODEL,
            system=system_prompt, timeout=6.0,
        )
        propositions = []
        for line in result.strip().splitlines():
            line = line.strip()
            # Flexible regex matching for P1/P2 format or bullet lines
            match = re.match(r"(?:P\d+:|\d+[\.\)]|\-|\*)\s*(.+?)(?:\|\s*TERMS?:\s*|:)(.+)", line, re.IGNORECASE)
            if match:
                prop_text = match.group(1).strip()
                terms = [t.strip().strip("'\"`") for t in match.group(2).split(",") if t.strip()]
                # Filter out generic words from extracted terms
                sharp_terms = [t for t in terms if t.lower() not in GENERIC_SEARCH_WORDS and len(t) >= 3]
                if not sharp_terms:
                    sharp_terms = terms[:3]
                if prop_text and sharp_terms:
                    propositions.append(SearchProposition(
                        proposition=prop_text,
                        code_terms=sharp_terms[:4],
                    ))
            elif ":" in line:
                parts = line.split(":", 1)
                terms = [t.strip().strip("'\"`") for t in parts[1].split(",") if t.strip()]
                sharp_terms = [t for t in terms if t.lower() not in GENERIC_SEARCH_WORDS and len(t) >= 3]
                if sharp_terms:
                    propositions.append(SearchProposition(
                        proposition=parts[0].strip(),
                        code_terms=sharp_terms[:4],
                    ))

        return propositions if len(propositions) >= 2 else _fallback_propositions(query, keywords)
    except Exception:
        return _fallback_propositions(query, keywords)


def _fallback_propositions(query: str, keywords: list) -> list:
    """Fallback: produce sharp technical primitive terms derived from query intent."""
    q_lower = query.lower()
    if "shell" in q_lower or "hack" in q_lower or "linux" in q_lower:
        return [
            SearchProposition(proposition="TTY pty execution dup2 descriptor", code_terms=["os.dup2", "pty.spawn", "socket"]),
            SearchProposition(proposition="Syscall ptrace process memory injection", code_terms=["ptrace", "mmap", "ctypes"]),
            SearchProposition(proposition="Process execution environment payload", code_terms=["execve", "subprocess.Popen", "os.execv"]),
        ]
    # Generic fallback: split query words and pair with low-level implementation keywords
    base_terms = [k for k in (keywords or query.split()) if k.lower() not in GENERIC_SEARCH_WORDS]
    if not base_terms:
        base_terms = [query]
    return [
        SearchProposition(proposition=f"{query} core implementation", code_terms=base_terms + ["impl"]),
        SearchProposition(proposition=f"{query} low level primitives", code_terms=base_terms + ["api"]),
        SearchProposition(proposition=f"{query} internal functions", code_terms=base_terms + ["handler"]),
    ]


async def _github_get(session, url: str, params: dict = None) -> dict:
    """GitHub API GET with semaphore, auth, and timeout."""
    headers = {"Accept": "application/vnd.github+json"}
    token = get_github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    async with _github_semaphore:
        async with session.get(url, headers=headers, params=params, timeout=GITHUB_TIMEOUT) as resp:
            return await resp.json()


GENERIC_SEARCH_WORDS = {
    "linux", "shell", "hacking", "python", "code", "script", "tool",
    "tutorial", "awesome", "example", "test", "demo", "helper", "utility",
    "hack", "payload", "exploit", "program", "app", "application"
}


async def search_github_code(session, expansion: SearchExpansion, language: str, limit: int) -> list:
    """Prong 1: Code Search API — find repos by what they CONTAIN.
    Uses unquoted search terms for maximum API recall across code symbols and imports."""
    if not expansion.code_terms:
        return []

    # Clean terms: strip module prefixes (os.dup2 -> dup2) to ensure search matches in raw source
    clean_terms = []
    for t in expansion.code_terms:
        t_clean = t.strip().strip("'\"`")
        if t_clean.lower() not in GENERIC_SEARCH_WORDS and len(t_clean) >= 3:
            clean_terms.append(t_clean)

    if not clean_terms:
        clean_terms = [t for t in expansion.code_terms if len(t) >= 3]

    if not clean_terms:
        return []

    # Join terms without restrictive outer quotes or invalid NOT path syntax
    terms = " ".join(clean_terms[:3])
    q = terms
    if language:
        ext_map = {"python": "py", "javascript": "js", "typescript": "ts",
                   "go": "go", "java": "java", "rust": "rs"}
        ext = ext_map.get(language.lower(), language.lower())
        q += f" extension:{ext}"

    try:
        data = await _github_get(session, "https://api.github.com/search/code",
                                 {"q": q, "per_page": limit})
        seen = set()
        repos = []
        for item in data.get("items", []):
            repo = item.get("repository", {})
            name = repo.get("full_name", "")
            if name and name not in seen:
                seen.add(name)
                repos.append(repo)
        return repos
    except Exception:
        return []


async def search_github_repos_v2(
    session, expansion: SearchExpansion, keywords: list,
    language: str, min_stars: int, max_stars: int, limit: int,
) -> list:
    """Prong 2: Topic-qualified Repo Search — find repos by categorization.
    Uses top topic for curated signal + main keywords + exclusions + star range + recency."""
    # To avoid over-constraining, pick top 1-2 topics or keywords
    q_parts = []

    # Primary search string: keywords or first topic
    if keywords:
        q_parts.append(" ".join(keywords[:3]))
    elif expansion.code_terms:
        q_parts.append(expansion.code_terms[0])

    if expansion.topics:
        q_parts.append(f"topic:{expansion.topics[0]}")

    # Exclusions
    for exc in expansion.exclude_topics[:2]:
        q_parts.append(f"-topic:{exc}")

    # Language filter
    if language:
        q_parts.append(f"language:{language}")

    # Star range (avoids both zero-quality and mega-popular generic repos)
    q_parts.append(f"stars:{min_stars}..{max_stars}")

    # Recency — only repos pushed in last 3 years
    from datetime import datetime, timedelta
    cutoff = (datetime.now() - timedelta(days=1095)).strftime("%Y-%m-%d")
    q_parts.append(f"pushed:>{cutoff}")

    q = " ".join(q_parts)

    try:
        data = await _github_get(session, "https://api.github.com/search/repositories",
                                 {"q": q, "per_page": limit})
        return data.get("items", [])
    except Exception:
        return []


async def fetch_full_repo_metadata(session, full_name: str) -> Optional[dict]:
    """Fetch full repository details from /repos/{owner}/{repo} to populate stars & default_branch."""
    try:
        return await _github_get(session, f"https://api.github.com/repos/{full_name}")
    except Exception:
        return None


async def discover_repos(
    session, query: str, keywords: list, language: str,
    min_stars: int, max_stars: int, max_repos: int,
    expansion: SearchExpansion,
    propositions: Optional[list] = None,
    debug: bool = False,
) -> list:
    """Multi-prong discovery: Proposition-based Code Searches + Standard Code Search + Topic Search.

    When deep propositions are available, fires N code searches (one per proposition)
    concurrently alongside the standard expansion-based search and topic search.
    All searches run in parallel — total latency ≈ slowest single search, not sum.
    """
    t0 = time.time()

    # Build all search tasks to fire concurrently
    search_tasks = []
    search_labels = []

    # Standard expansion-based code search (always runs)
    search_tasks.append(search_github_code(session, expansion, language, max_repos * 2))
    search_labels.append("expansion_code")

    # Topic-qualified repo search (always runs)
    search_tasks.append(search_github_repos_v2(
        session, expansion, keywords, language, min_stars, max_stars, max_repos * 2,
    ))
    search_labels.append("topic_repo")

    # Deep proposition code searches (one per proposition, if available)
    if propositions:
        for i, prop in enumerate(propositions[:5]):
            # Create a mini-expansion from each proposition's code terms
            prop_expansion = SearchExpansion(
                code_terms=prop.code_terms,
                topics=[], exclude_topics=[], extra_extensions=[],
            )
            search_tasks.append(search_github_code(session, prop_expansion, language, max_repos))
            search_labels.append(f"prop_{i}:{prop.proposition[:40]}")

    # Fire ALL searches concurrently
    results = await asyncio.gather(*search_tasks, return_exceptions=True)

    # Collect all repos, dedup by full_name
    seen = {}
    for label, result in zip(search_labels, results):
        if isinstance(result, Exception):
            if debug:
                print(f"[discovery] {label} FAILED: {result}")
            continue
        repos = result if isinstance(result, list) else []
        for repo in repos:
            name = repo.get("full_name", "")
            if name and name not in seen:
                seen[name] = repo

    raw_merged = list(seen.values())

    if debug:
        counts = {}
        for label, result in zip(search_labels, results):
            if not isinstance(result, Exception):
                counts[label] = len(result) if isinstance(result, list) else 0
        print(f"[discovery] search counts: {counts}")

    # Code Search repos lack 'stargazers_count'! Fetch full metadata for repos missing it.
    missing_meta = [r.get("full_name") for r in raw_merged if "stargazers_count" not in r]
    if missing_meta:
        if debug:
            print(f"[discovery] fetching full metadata for {len(missing_meta)} code-search repos...")
        meta_results = await asyncio.gather(
            *[fetch_full_repo_metadata(session, name) for name in missing_meta[:20]],
            return_exceptions=True
        )
        meta_dict = {
            m.get("full_name"): m for m in meta_results
            if isinstance(m, dict) and m.get("full_name")
        }
        for i, r in enumerate(raw_merged):
            fname = r.get("full_name")
            if fname in meta_dict:
                raw_merged[i] = meta_dict[fname]

    # Apply metadata filter
    filtered = metadata_filter(raw_merged, min_stars, max_stars)

    if debug:
        print(f"[discovery] {time.time()-t0:.2f}s | merged={len(raw_merged)}, "
              f"after filter={len(filtered)}, propositions={len(propositions) if propositions else 0}")

    return filtered[:max_repos]


# ============================================================================
# 4. METADATA FILTERING
# ============================================================================

def metadata_filter(repos: list, min_stars: int, max_stars: int) -> list:
    """Filter repos by star range, skip archived repos."""
    filtered = []
    for r in repos:
        stars = r.get("stargazers_count", 0)
        if stars < min_stars:
            continue
        if max_stars and stars > max_stars:
            continue
        if r.get("archived"):
            continue
        filtered.append(r)
    return filtered


# ============================================================================
# 5. FILE DISCOVERY — repo tree, file selection, content fetch
# ============================================================================

BASE_EXTENSIONS = r"\.(py|js|ts|go|java|rs)$"

LOW_SIGNAL_PATH = re.compile(
    r"(readme|license|contributing|setup\.py|conftest\.py|__init__\.py|"
    r"docs?/|examples?/|changelog|venv/|build/|\.git/|node_modules/|dist/|vendor/|"
    r"\.test\.[a-z]+$|\.spec\.[a-z]+$|_test\.[a-z]+$|^tests?/)",
    re.IGNORECASE,
)


async def get_repo_tree(session, full_name: str, default_branch: str) -> list:
    """Fetch repo tree with persistent SQLite + in-memory TTL cache (1 hour)."""
    cache_key = f"{full_name}:{default_branch}"
    if cache_key in _TREE_CACHE:
        cached_tree, cached_ts = _TREE_CACHE[cache_key]
        if time.time() - cached_ts < TREE_CACHE_TTL:
            return cached_tree

    db_tree = _db_get_tree(cache_key, ttl=TREE_CACHE_TTL)
    if db_tree is not None:
        _TREE_CACHE[cache_key] = (db_tree, time.time())
        return db_tree

    url = f"https://api.github.com/repos/{full_name}/git/trees/{default_branch}?recursive=1"
    data = await _github_get(session, url)
    tree = data.get("tree", [])
    _TREE_CACHE[cache_key] = (tree, time.time())
    _db_set_tree(cache_key, tree)
    return tree


async def fetch_raw_file(session, full_name: str, branch: str, path: str) -> str:
    url = f"https://raw.githubusercontent.com/{full_name}/{branch}/{path}"
    try:
        async with session.get(url, timeout=GITHUB_TIMEOUT) as resp:
            if resp.status != 200:
                return ""
            return await resp.text()
    except Exception:
        return ""


async def select_relevant_files(
    llm_call_fn, query: str, paths: list, max_files: int,
    readme_excerpt: str = "",
) -> list:
    """LLM picks the most relevant files from a list of paths.
    Uses the repo map pattern — hand the LLM paths (no content, cheap)
    and let it reason about which ones are actually relevant."""
    if not paths:
        return []
    path_list = "\n".join(paths[:300])
    readme_block = (f"README excerpt:\n{readme_excerpt}\n\n" if readme_excerpt else "")
    prompt = (
        f"Query: {query}\n\n{readme_block}"
        f"File paths:\n{path_list}\n\n"
        f"List the {max_files} file paths most likely to contain core functional code implementing the query.\n"
        f"RULES:\n"
        f"1. Select files with actual implementation logic (core engines, payload generators, socket/pty/process execution).\n"
        f"2. Explicitly DISCARD installation scripts, setup scripts, CLI UI menus, argument parsers, or tool data dictionaries (e.g. `installation_logic.py`, `tool_data.py`, `setup.py`, `menu.py`).\n"
        f"3. Output ONLY the {max_files} paths, one per line, exactly as given — no explanation, no numbering."
    )
    result = await llm_call_fn(
        prompt, max_tokens=150, model=NVIDIA_CONTEXT_MODEL,
        system="detailed thinking off",
    )
    picked = [line.strip() for line in result.strip().splitlines() if line.strip()]
    valid = [p for p in picked if p in paths]
    return valid[:max_files] if valid else paths[:max_files]


# ============================================================================
# 6. SOURCE CLEANING — deterministic pre-processing before chunking
# ============================================================================

LICENSE_HEADER = re.compile(
    r"copyright|permission is hereby granted|licensed under|"
    r"spdx-license-identifier|apache license|mit license|gnu general public",
    re.IGNORECASE,
)

BOILERPLATE_LINE = re.compile(
    r"^\s*("
    r"print\(|(?:raw_)?input\(|"
    r"console\.log\(|console\.error\(|"
    r"fmt\.Print|fmt\.Sprint|"
    r"System\.out\.print|System\.err\.print|"
    r"println!|print!|eprintln!|"
    r"colored\(|cprint\(|console\.print\(|"
    r"Progress\(|Spinner\(|tqdm\(|"
    r"pyfiglet|figlet_format|"
    r"#.*banner|ascii_art|menu\[|choice\s*=|"
    r"sys\.stdout\.write\(|sys\.stdout\.flush\(|"
    r"check_shell_args\(|parse_args\(|ArgumentParser\(|"
    r"time\.sleep\(|stdout\.write\('\\r"
    r")",
    re.IGNORECASE,
)


def strip_license_header(content: str) -> str:
    """Strip leading license/copyright header block."""
    lines = content.splitlines()
    end = 0
    for i, line in enumerate(lines[:60]):
        stripped = line.strip()
        if stripped == "" or stripped.startswith(("#", "//", "/*", "*", "'''", '"""')):
            end = i + 1
        else:
            break
    header_block = "\n".join(lines[:end])
    if LICENSE_HEADER.search(header_block):
        return "\n".join(lines[end:])
    return content


def strip_ascii_banners(content: str) -> str:
    """Remove long non-code blocks (ASCII art/banners) with low letter density."""
    def _maybe_strip(match):
        block = match.group(0)
        if block.count("\n") < 5:
            return block
        letters = sum(c.isalpha() for c in block)
        density = letters / max(len(block), 1)
        return "" if density < 0.35 else block

    content = re.sub(r'("""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\')', _maybe_strip, content)
    content = re.sub(r"/\*[\s\S]*?\*/", _maybe_strip, content)
    content = re.sub(r"(?:^\s*//.*\n){6,}", _maybe_strip, content, flags=re.MULTILINE)
    return content


def strip_boilerplate_lines(text: str) -> str:
    """Remove individual noise lines (print/input/menu) from within a chunk."""
    return "\n".join(line for line in text.splitlines() if not BOILERPLATE_LINE.match(line))


def clean_source(content: str) -> str:
    """Full deterministic cleaning pipeline applied before chunking."""
    content = strip_license_header(content)
    content = strip_ascii_banners(content)
    content = "\n".join(line.rstrip() for line in content.splitlines())
    content = re.sub(r"\n{3,}", "\n\n", content)
    return content


# ============================================================================
# 7. AST EXTRACTION — structured node extraction with comment attachment
# ============================================================================

def _get_preceding_comments(lines: list, node_start_line: int) -> str:
    """Walk backwards from an AST node to capture the contiguous comment
    block immediately above it. Stops at blank lines or code."""
    comments = []
    i = node_start_line - 1  # 0-indexed, line above the node
    while i >= 0:
        stripped = lines[i].strip()
        if stripped.startswith("#") or stripped.startswith("//"):
            comments.append(lines[i])
            i -= 1
        elif stripped == "":
            # Allow one blank line between comment and node
            if i > 0 and lines[i - 1].strip().startswith(("#", "//")):
                i -= 1
            else:
                break
        else:
            break
    comments.reverse()
    return "\n".join(comments).strip()


def extract_ast_nodes_python(content: str) -> list:
    """Extract top-level Python AST nodes with comment attachment."""
    if len(content.strip()) < MIN_FILE_CHARS:
        return []
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return _extract_regex_fallback(content)

    lines = content.splitlines()
    nodes = []

    for i, node in enumerate(tree.body):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue

        try:
            source = ast.unparse(node)
        except Exception:
            continue

        if len(source.splitlines()) <= 2:
            continue

        node_type = {
            ast.FunctionDef: "function",
            ast.AsyncFunctionDef: "async_function",
            ast.ClassDef: "class",
        }[type(node)]

        # Line numbers
        start_line = node.lineno - 1  # 0-indexed
        if hasattr(node, 'decorator_list') and node.decorator_list:
            start_line = node.decorator_list[0].lineno - 1
        preceding = _get_preceding_comments(lines, start_line)

        end_line = node.end_lineno if hasattr(node, 'end_lineno') and node.end_lineno else start_line + len(source.splitlines())

        ast_node = ASTNode(
            index=len(nodes),
            node_type=node_type,
            name=node.name,
            source=source,
            preceding_comments=preceding,
            line_range=(start_line + 1, end_line),
            line_count=len(source.splitlines()),
        )
        nodes.append(ast_node)

    if not nodes:
        return _extract_regex_fallback(content)
    return nodes


# Tree-sitter support (optional soft dependency)
_TS_PARSERS = {}
TS_NODE_TYPES = {
    "js": {"function_declaration", "method_definition", "class_declaration",
           "arrow_function", "export_statement"},
    "go": {"function_declaration", "method_declaration"},
    "java": {"method_declaration", "class_declaration"},
    "rust": {"function_item", "impl_item"},
}
TS_NAME_FIELDS = {"name", "declarator"}

EXT_TO_TS_KEY = {".js": "js", ".ts": "js", ".go": "go", ".java": "java", ".rs": "rust"}


def _setup_treesitter():
    try:
        from tree_sitter_languages import get_parser
    except ImportError:
        return
    probes = {
        "js": (b"function f(){}", "javascript"),
        "go": (b"func f(){}", "go"),
        "java": (b"class C { void f(){} }", "java"),
        "rust": (b"fn f(){}", "rust"),
    }
    for ext_key, (probe_bytes, ts_lang_name) in probes.items():
        try:
            parser = get_parser(ts_lang_name)
            result = parser.parse(probe_bytes)
            if result.root_node.child_count > 0:
                _TS_PARSERS[ext_key] = parser
        except Exception:
            continue

_setup_treesitter()


def _ts_get_name(node) -> str:
    """Try to extract a name from a tree-sitter node."""
    for field in TS_NAME_FIELDS:
        child = node.child_by_field_name(field)
        if child:
            return child.text.decode("utf-8", errors="replace")
    return f"anonymous_{node.type}"


def extract_ast_nodes_treesitter(content: str, ext_key: str) -> Optional[list]:
    """Extract top-level AST nodes via tree-sitter. Returns None if unavailable."""
    parser = _TS_PARSERS.get(ext_key)
    if not parser or len(content.strip()) < MIN_FILE_CHARS:
        return None
    try:
        tree = parser.parse(content.encode("utf-8"))
        node_types = TS_NODE_TYPES.get(ext_key, set())
        lines = content.splitlines()
        nodes = []

        for ts_node in tree.root_node.children:
            if ts_node.type not in node_types:
                continue
            source = content[ts_node.start_byte:ts_node.end_byte]
            if source.count("\n") <= 1:
                continue

            name = _ts_get_name(ts_node)
            start_line = ts_node.start_point[0]
            preceding = _get_preceding_comments(lines, start_line)

            ast_node = ASTNode(
                index=len(nodes),
                node_type=ts_node.type,
                name=name,
                source=source,
                preceding_comments=preceding,
                line_range=(start_line + 1, ts_node.end_point[0] + 1),
                line_count=source.count("\n") + 1,
            )
            nodes.append(ast_node)

        return nodes if nodes else None
    except Exception:
        return None


# Regex fallback for when AST/tree-sitter isn't available
FUNC_BOUNDARY = re.compile(
    r"^(?:def |class |async def |function |func |public |private |protected )"
)


def _extract_regex_fallback(content: str) -> list:
    """Fallback: regex-based function/class boundary extraction."""
    if len(content.strip()) < MIN_FILE_CHARS:
        return []

    lines = content.splitlines()
    boundaries = []
    in_docstring = False

    for i, line in enumerate(lines):
        if re.search(r'("""|\'\'\')', line):
            in_docstring = not in_docstring
            continue
        if in_docstring:
            continue
        if FUNC_BOUNDARY.match(line.strip()):
            start = i
            while start > 0 and lines[start - 1].strip().startswith("@"):
                start -= 1
            if start not in boundaries:
                boundaries.append(start)

    boundaries = sorted(set(boundaries))

    if not boundaries:
        # Whole file as one chunk if small enough
        if len(content) <= 6000:
            return [ASTNode(0, "file", "whole_file", content, "", (1, len(lines)), len(lines))]
        return []

    nodes = []
    for idx, start in enumerate(boundaries):
        end = boundaries[idx + 1] if idx + 1 < len(boundaries) else len(lines)
        source = "\n".join(lines[start:end]).strip()
        if not source:
            continue

        # Try to extract name from first line
        first_line = lines[start].strip()
        name_match = re.search(r"(?:def|class|function|func)\s+(\w+)", first_line)
        name = name_match.group(1) if name_match else f"block_{idx}"

        preceding = _get_preceding_comments(lines, start)
        nodes.append(ASTNode(
            index=len(nodes), node_type="function", name=name,
            source=source, preceding_comments=preceding,
            line_range=(start + 1, end), line_count=end - start,
        ))

    return nodes


def extract_ast_nodes(content: str, path: str) -> list:
    """Unified AST extraction entry point — picks the right parser."""
    if path.endswith(".py"):
        return extract_ast_nodes_python(content)

    ts_key = next((k for ext, k in EXT_TO_TS_KEY.items() if path.endswith(ext)), None)
    if ts_key:
        nodes = extract_ast_nodes_treesitter(content, ts_key)
        if nodes:
            return nodes

    return _extract_regex_fallback(content)


# ============================================================================
# 8. HYBRID AST + LLM CHUNKING — ONE LLM call per file
# ============================================================================

async def llm_refine_chunks(
    llm_call_fn, query: str, nodes: list, repo: str, path: str,
    full_file_content: str,
) -> list:
    """Single LLM call per file that simultaneously:
    - Decides which AST nodes are relevant (replaces quick_relevance_filter)
    - Decides grouping (merge small related functions into 1 chunk)
    - Generates a context sentence per kept node (replaces contextualize_chunk)

    Output format:
      KEEP[0]: CONTEXT: <one sentence>
      KEEP[2,3]: MERGE, CONTEXT: <one sentence for merged chunk>
      DROP[1]: boilerplate
    """
    if not nodes:
        return []

    # Build node listing for the LLM
    node_listing = "\n".join(
        f"[{n.index}] {n.node_type} '{n.name}' ({n.line_count} lines)"
        + (f"\n    Comments: {n.preceding_comments[:100]}" if n.preceding_comments else "")
        + f"\n    Preview: {n.source[:120]}..."
        for n in nodes
    )

    prompt = (
        f"Query: {query}\n"
        f"Repo: {repo}, File: {path}\n\n"
        f"AST nodes extracted from this file:\n{node_listing}\n\n"
        f"For each node, decide: KEEP, MERGE (with adjacent node), or DROP.\n"
        f"DROP any node that is UI/menu/banner/boilerplate/installer/argparse/CLI "
        f"scaffolding — regardless of library or naming convention.\n"
        f"For KEPT nodes, provide a CONTEXT sentence (under 25 words) describing "
        f"what the code does, for embedding enrichment.\n\n"
        f"Output ONLY in this format, one line per decision:\n"
        f"KEEP[0]: CONTEXT: Implements reverse TCP connection using socket library\n"
        f"KEEP[2,3]: MERGE, CONTEXT: Helper functions for payload encoding\n"
        f"DROP[1]: argument parser boilerplate\n"
        f"Nothing else."
    )

    try:
        result = await llm_call_fn(
            prompt, max_tokens=200, model=NVIDIA_CONTEXT_MODEL,
            system="detailed thinking off", timeout=4.5,
        )
        return _parse_refine_output(result, nodes)
    except Exception:
        # Fallback: keep all nodes with no context enrichment
        return [CodeChunk(
            repo=repo, path=path,
            content=(n.preceding_comments + "\n\n" if n.preceding_comments else "") + n.source,
            stars=0,
        ) for n in nodes]


def _parse_refine_output(result: str, nodes: list) -> list:
    """Parse the LLM refinement output into CodeChunk objects."""
    chunks = []
    node_map = {n.index: n for n in nodes}

    for line in result.strip().splitlines():
        line = line.strip()
        if not line:
            continue

        # Parse KEEP[indices]: ...
        keep_match = re.match(r"KEEP\[([0-9,\s]+)\]:\s*(.*)", line, re.IGNORECASE)
        if not keep_match:
            continue

        indices_str, rest = keep_match.groups()
        try:
            indices = [int(x.strip()) for x in indices_str.split(",")]
        except ValueError:
            continue

        # Extract context
        context = ""
        ctx_match = re.search(r"CONTEXT:\s*(.+)", rest, re.IGNORECASE)
        if ctx_match:
            context = ctx_match.group(1).strip()[:220]

        # Build chunk content from indicated nodes
        source_parts = []
        repo_name = ""
        path_name = ""
        for idx in indices:
            node = node_map.get(idx)
            if not node:
                continue
            if node.preceding_comments:
                source_parts.append(node.preceding_comments)
            source_parts.append(node.source)

        if not source_parts:
            continue

        content = "\n\n".join(source_parts)
        content = strip_boilerplate_lines(content)
        if not content.strip():
            continue

        # Prepend context for embedding enrichment
        if context:
            content = f"{context}\n\n{content}"

        # Use first node's info for metadata
        first_node = node_map.get(indices[0])
        chunks.append(CodeChunk(
            repo="", path="",  # filled by caller
            content=content, stars=0,
        ))

    return chunks


async def hybrid_chunk(
    llm_call_fn, query: str, content: str, repo: str, path: str,
    debug: bool = False,
) -> list:
    """Unified entry point: clean → AST extract → LLM refine → chunks.
    ONE LLM call per file replaces the old quick_relevance_filter +
    contextualize_chunk (which were TWO separate LLM stages)."""
    content = clean_source(content)
    if len(content.strip()) < MIN_FILE_CHARS:
        return []

    # AST extraction (CPU work — runs while other repos' I/O awaits)
    nodes = extract_ast_nodes(content, path)
    if debug:
        print(f"  [{repo}/{path}] AST extracted {len(nodes)} nodes")

    if not nodes:
        return []

    # Fast-path AST ceiling: for large files (>15 nodes), apply strict AST function filtering
    # instead of dumping raw unrefined boilerplate nodes.
    if len(nodes) > 15:
        if debug:
            print(f"  [{repo}/{path}] >15 nodes ({len(nodes)}) → filtering boilerplate nodes via AST heuristics")
        # Keep only functions with >3 lines and non-boilerplate names
        filtered_nodes = [
            n for n in nodes
            if len(n.source.splitlines()) > 3
            and not re.search(r"^(main|parse_args|print|show_menu|banner|spinner|logo|help)$", n.name, re.IGNORECASE)
        ]
        target_nodes = filtered_nodes if filtered_nodes else nodes[:15]
        return [CodeChunk(
            repo=repo, path=path,
            content=strip_boilerplate_lines(
                (n.preceding_comments + "\n\n" if n.preceding_comments else "") + n.source
            ),
            stars=0,
        ) for n in target_nodes if n.source.strip()]

    # LLM refinement — ONE fast call (2.5s cap) for this entire file
    chunks = await llm_refine_chunks(llm_call_fn, query, nodes, repo, path, content)

    # Fill in metadata
    for c in chunks:
        c.repo = repo
        c.path = path

    if debug:
        print(f"  [{repo}/{path}] LLM refined → {len(chunks)} chunks")

    # Fallback: if LLM returned nothing, keep all nodes as raw chunks
    if not chunks and nodes:
        chunks = [CodeChunk(
            repo=repo, path=path,
            content=strip_boilerplate_lines(
                (n.preceding_comments + "\n\n" if n.preceding_comments else "") + n.source
            ),
            stars=0,
        ) for n in nodes if len(n.source.splitlines()) > 2]
        # Filter empty chunks
        chunks = [c for c in chunks if c.content.strip()]

    return chunks


# ============================================================================
# 9. MULTI-QUERY EXPANSION — replaces HyDE
# ============================================================================

async def expand_query_for_retrieval(
    llm_call_fn, query: str, keywords: list,
) -> list:
    """Generate technical query variations for multi-vector retrieval.
    Target exact API methods, syscalls, and low-level code primitives, strictly
    avoiding high-level CLI tool names or framework wrappers."""
    prompt = (
        f"Original query: {query}\n"
        f"Keywords: {', '.join(keywords) if keywords else 'none'}\n\n"
        "Rewrite this as 3 different SHORT technical code search queries (under 12 words each).\n"
        "RULES:\n"
        "1. Focus ONLY on exact API method names, low-level data structures, or code primitives.\n"
        "2. Do NOT mention high-level tool names, CLI framework wrappers, or generic scripts.\n"
        "3. Output ONLY the 3 queries, one per line, nothing else."
    )
    try:
        result = await llm_call_fn(
            prompt, max_tokens=100, model=NVIDIA_CONTEXT_MODEL,
            system="detailed thinking off",
        )
        raw_lines = [line.strip() for line in result.strip().splitlines() if line.strip()]
        variations = []
        for line in raw_lines:
            # Strip 1., 2), -, * prefixes
            cleaned = re.sub(r"^(?:\d+[\.\)]|[\-\*])\s*", "", line).strip()
            if cleaned:
                variations.append(cleaned)
        return [query] + variations[:3]
    except Exception:
        return [query]  # fallback: just the original query


# ============================================================================
# 10. EMBEDDING — NVIDIA Nemotron, batched, with normalization
# ============================================================================

# Counter for round-robin embed key selection
_embed_key_counter = 0


async def _embed_batch(session, texts: list, input_type: str) -> list:
    """Embed a single batch of texts. Cycles across available API keys
    round-robin so 2 keys from different accounts = 2× embed throughput."""
    global _embed_key_counter
    embed_keys = get_embed_keys()
    key = embed_keys[_embed_key_counter % len(embed_keys)]
    _embed_key_counter += 1

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": NVIDIA_EMBED_MODEL,
        "input": texts,
        "input_type": input_type,
        "encoding_format": "float",
    }

    async def _call():
        async with session.post(NVIDIA_EMBED_URL, headers=headers, json=payload,
                                timeout=aiohttp.ClientTimeout(total=EMBED_TIMEOUT)) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"{resp.status} embedding API error: {body[:300]}")
            data = await resp.json()
            return [d["embedding"] for d in data["data"]]

    return await call_with_backoff(_call)


def _normalize_vec(v: list, dim: int = EMBED_DIM) -> list:
    """Truncate to dim + L2 re-normalize (Matryoshka requirement)."""
    sliced = v[:dim]
    norm = math.sqrt(sum(x * x for x in sliced)) or 1.0
    return [x / norm for x in sliced]


async def _embed_batch_cached(session, texts: list, input_type: str) -> list:
    """Embedding with multi-layer in-memory + SQLite SHA-256 cache."""
    results = [None] * len(texts)
    uncached_texts = []
    uncached_indices = []

    for i, t in enumerate(texts):
        h = _cache_key_embed(t)
        if h in _EMBED_CACHE:
            results[i] = _EMBED_CACHE[h]
        else:
            db_vec = _db_get_embed(h)
            if db_vec is not None:
                _EMBED_CACHE[h] = db_vec
                results[i] = db_vec
            else:
                uncached_texts.append(t)
                uncached_indices.append(i)

    if uncached_texts:
        vecs = await _embed_batch(session, uncached_texts, input_type)
        for idx, vec in zip(uncached_indices, vecs):
            h = _cache_key_embed(texts[idx])
            norm_vec = _normalize_vec(vec)
            _EMBED_CACHE[h] = norm_vec
            _db_set_embed(h, norm_vec)
            results[idx] = norm_vec

    return results


async def embed_texts(
    session, texts: list, input_type: str = "passage", dim: int = EMBED_DIM,
) -> list:
    """Embed texts in batches with multi-layer caching + truncation + normalization."""
    all_vectors = []
    for i in range(0, len(texts), EMBED_BATCH_SIZE):
        batch = texts[i:i + EMBED_BATCH_SIZE]
        vectors = await _embed_batch_cached(session, batch, input_type)
        all_vectors.extend(vectors)
    return vectors if len(texts) <= EMBED_BATCH_SIZE else all_vectors


def cosine_sim(a: list, b: list) -> float:
    """Vectorized dot product of L2-normalized vectors using NumPy."""
    if not a or not b:
        return 0.0
    va = np.array(a[:EMBED_DIM], dtype=np.float32)
    vb = np.array(b[:EMBED_DIM], dtype=np.float32)
    norm_a = np.linalg.norm(va) or 1.0
    norm_b = np.linalg.norm(vb) or 1.0
    return float(np.dot(va, vb) / (norm_a * norm_b))


# ============================================================================
# 11. CHUNK DEDUPLICATION — SHA-256 exact + near-duplicate + path dedup
# ============================================================================

def deduplicate_chunks(chunks: list) -> list:
    """Two-level deduplication applied BEFORE embedding (saves API calls).

    Level 1: Content hash — exact-duplicate chunks collapsed.
    Level 2: Near-duplicate — chunks from same repo+path with >85% overlap
             keep only the longer one.
    """
    if not chunks:
        return []

    # Level 1: exact content hash
    seen_hashes = set()
    unique = []
    for c in chunks:
        h = hashlib.sha256(c.content.strip().encode()).hexdigest()
        if h not in seen_hashes:
            seen_hashes.add(h)
            unique.append(c)

    # Level 2: near-duplicate within same file
    by_path = {}
    for c in unique:
        key = (c.repo, c.path)
        by_path.setdefault(key, []).append(c)

    final = []
    for key, group in by_path.items():
        group.sort(key=lambda c: len(c.content), reverse=True)
        kept = []
        for c in group:
            is_near_dup = any(
                SequenceMatcher(None, c.content[:500], existing.content[:500]).ratio() > 0.85
                for existing in kept
            )
            if not is_near_dup:
                kept.append(c)
        final.extend(kept)

    return final


def path_dedup_topn(chunks: list) -> list:
    """Post-rerank: at most 1 chunk per repo+path in final output."""
    seen = set()
    result = []
    for c in chunks:
        key = (c.repo, c.path)
        if key not in seen:
            seen.add(key)
            result.append(c)
    return result


# ============================================================================
# 12. RERANKING — RRF (Reciprocal Rank Fusion) of dense + BM25
# ============================================================================

RRF_K = 60  # Standard RRF constant


def rrf_score(rank: int, k: int = RRF_K) -> float:
    """Reciprocal Rank Fusion score."""
    return 1.0 / (k + rank)


def rerank_rrf(chunks: list, query_vecs: list, query: str, top_n: int) -> list:
    """Hybrid reranking: RRF fusion of dense cosine + BM25 sparse rankings.
    RRF is more robust than linear combination without per-dataset tuning."""
    if not chunks:
        return []

    # Dense ranking: max cosine similarity across all query variations
    for c in chunks:
        c.score = max(cosine_sim(c.embedding, qv) for qv in query_vecs)
    dense_ranked = sorted(range(len(chunks)), key=lambda i: chunks[i].score, reverse=True)

    # Sparse ranking: BM25
    tokenized_corpus = [c.content.lower().split() for c in chunks]
    bm25 = BM25Okapi(tokenized_corpus)
    sparse_scores = bm25.get_scores(query.lower().split())
    sparse_ranked = sorted(range(len(chunks)), key=lambda i: sparse_scores[i], reverse=True)

    # Build rank lookup
    dense_rank = {idx: rank for rank, idx in enumerate(dense_ranked)}
    sparse_rank = {idx: rank for rank, idx in enumerate(sparse_ranked)}

    # RRF fusion
    for i, c in enumerate(chunks):
        c.score = rrf_score(dense_rank[i]) + rrf_score(sparse_rank[i])

    chunks.sort(key=lambda c: c.score, reverse=True)
    return chunks[:top_n]


# ============================================================================
# 13. LLM JUDGE — final semantic elimination
# ============================================================================

async def judge_chunks(
    llm_call_fn, query: str, chunks: list, keep_n: int,
    session: Optional[aiohttp.ClientSession] = None,
) -> tuple:
    """'Extract & Stitch' LLM Reranker & Extraction Engine with NVIDIA Rerank API acceleration.

    Speed & Precision Tier:
    1. NVIDIA Rerank API (/v1/ranking): ~100 ms HTTP call with strict logit threshold (>= -2.0)
       to instantly purge noise (CLI/argparse/print loops/scrapers).
    2. LLM Extract & Stitch fallback: single-pass LLM extraction and stitching.

    Returns (kept_chunks, used_fallback: bool).
    """
    if not chunks:
        return chunks, False

    # ── Stage 1: NVIDIA Rerank API (~100 ms) with Logit Threshold Filter ──────
    if session is not None:
        try:
            api_keys = get_all_api_keys()
            if api_keys:
                headers = {
                    "Authorization": f"Bearer {api_keys[0]}",
                    "Content-Type": "application/json",
                }
                payload = {
                    "model": "nvidia/rerank-qa-mistral-4b",
                    "query": {"text": query},
                    "passages": [{"text": f"File: {c.path}\n{c.content[:1000]}"} for c in chunks],
                }
                async with session.post(
                    "https://integrate.api.nvidia.com/v1/ranking",
                    headers=headers,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=3.0),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        rankings = data.get("rankings", data.get("data", []))
                        for r in rankings:
                            idx = r.get("index")
                            score = r.get("logit", r.get("score", 0.0))
                            if idx is not None and 0 <= idx < len(chunks):
                                chunks[idx].score = float(score)
                        # Dynamic logit threshold: keep top scores above mean or -2.0, preventing over-filtering
                        scores = [c.score for c in chunks]
                        mean_score = float(np.mean(scores)) if scores else -2.0
                        cutoff = max(mean_score, -2.5)
                        filtered_chunks = [c for c in chunks if c.score >= cutoff]
                        if not filtered_chunks:
                            filtered_chunks = chunks
                        ranked = sorted(filtered_chunks, key=lambda c: c.score, reverse=True)
                        return ranked[:keep_n], False
        except Exception:
            pass

    # ── Stage 2: LLM Extract & Stitch Fallback ──────────────────────────────
    listing = "\n\n".join(
        f"--- CANDIDATE [{i}] (repo={c.repo}, path={c.path}) ---\n{c.content[:700]}"
        for i, c in enumerate(chunks)
    )
    system_prompt = (
        "You are an expert Code Extraction & Stitching Engine. Your task is to analyze candidate code "
        "chunks for a user query and perform surgical EXTRACT & STITCH:\n\n"
        "1. EVALUATE & FILTER: Examine each candidate chunk. Completely SKIP any chunk that is generic "
        "CLI boilerplate, argument parsing (argparse/click), interactive menus, or web scraper noise.\n"
        "2. SURGICAL EXTRACTION: If a chunk is relevant, extract ONLY the exact functional code lines "
        "implementing the target logic. Do NOT copy surrounding boilerplate.\n"
        "3. STITCHING & FORMATTING: Output each extracted chunk with its index and context summary.\n\n"
        "Output format for each relevant chunk:\n"
        "CHUNK[index]: <1-line technical summary>\n"
        "```<lang>\n"
        "<exact extracted code snippet>\n"
        "```\n\n"
        "If no candidate chunks contain relevant code, output 'NONE'."
    )
    prompt = (
        f"User Query: {query}\n\n"
        f"Candidate Code Chunks:\n\n{listing}\n\n"
        f"Perform Extract & Stitch for up to {keep_n} relevant chunks. Output ONLY formatted CHUNK[index] blocks:"
    )

    try:
        result = await llm_call_fn(
            prompt, max_tokens=600, model=NVIDIA_CHAT_MODEL,
            system=system_prompt, timeout=4.0,
        )
        if "NONE" in result.upper() and "CHUNK" not in result.upper():
            return [], False

        stitched_chunks = []
        seen_indices = set()

        # Parse CHUNK[index]: summary \n ```lang \n code \n ```
        blocks = re.split(r"CHUNK\[(\d+)\]:\s*", result, flags=re.IGNORECASE)
        for i in range(1, len(blocks) - 1, 2):
            idx_str = blocks[i]
            block_content = blocks[i + 1].strip()
            if not idx_str.isdigit():
                continue
            idx = int(idx_str)
            if 0 <= idx < len(chunks) and idx not in seen_indices:
                seen_indices.add(idx)
                orig_chunk = chunks[idx]

                # Extract code inside ``` ... ``` if present
                code_match = re.search(r"```(?:\w+)?\n?(.*?)\n?```", block_content, re.DOTALL)
                extracted_code = code_match.group(1).strip() if code_match else block_content.strip()

                # Extract context summary (text before code block)
                summary = block_content.split("```")[0].strip() if "```" in block_content else ""

                if extracted_code:
                    final_content = f"{summary}\n\n{extracted_code}" if summary else extracted_code
                    stitched_chunks.append(CodeChunk(
                        repo=orig_chunk.repo,
                        path=orig_chunk.path,
                        content=final_content,
                        stars=orig_chunk.stars,
                        score=orig_chunk.score,
                    ))

        if stitched_chunks:
            return stitched_chunks[:keep_n], False

        # Fallback 1: match CHUNK[index] line references if code block parsing didn't split cleanly
        for line in result.strip().splitlines():
            line = line.strip()
            m = re.match(r"(?:CHUNK|KEEP)\[(\d+)\]:\s*(.*)", line, re.IGNORECASE)
            if m:
                idx = int(m.group(1))
                if 0 <= idx < len(chunks) and idx not in seen_indices:
                    seen_indices.add(idx)
                    stitched_chunks.append(chunks[idx])

        if stitched_chunks:
            return stitched_chunks[:keep_n], False

        # Fallback 2: raw index list
        indices = [int(x) for x in re.findall(r"\b\d+\b", result) if 0 <= int(x) < len(chunks)]
        picked = [chunks[i] for i in dict.fromkeys(indices)]
        if picked:
            return picked[:keep_n], False

        return chunks[:keep_n], True
    except Exception:
        return chunks[:keep_n], True


# ============================================================================
# 14. PER-REPO PIPELINE — fully parallel, independent per repo
# ============================================================================

async def process_repo(
    session, repo: dict, query: str, llm_call_fn,
    ext_pattern: str, max_files: int, chunk_cap: int,
    debug: bool = False,
) -> list:
    """Complete pipeline for one repo: tree → file select → fetch → chunk.
    Runs independently and concurrently with other repos. CPU work (AST, regex)
    interleaves with I/O waits (API calls) for other repos via the event loop."""
    t0 = time.time()
    full_name = repo.get("full_name", "unknown")
    default_branch = repo.get("default_branch", "main")
    stars = repo.get("stargazers_count", 0)

    try:
        # --- Tree fetch (I/O) ---
        tree = await get_repo_tree(session, full_name, default_branch)
        if debug:
            print(f"[{full_name}] tree: {len(tree)} entries ({time.time()-t0:.2f}s)")

        # --- README for file selection context (I/O) ---
        readme_path = next(
            (e.get("path", "") for e in tree
             if re.match(r"^readme\.(md|rst|txt)$", e.get("path", "").split("/")[-1], re.IGNORECASE)),
            None,
        )
        readme_excerpt = ""
        if readme_path:
            readme_content = await fetch_raw_file(session, full_name, default_branch, readme_path)
            readme_excerpt = readme_content[:600] if readme_content else ""

        # --- Path filtering (CPU — runs while other repos await I/O) ---
        paths = [
            e.get("path", "") for e in tree
            if e.get("type") == "blob"
            and re.search(ext_pattern, e.get("path", ""))
            and not re.search(r"(test|vendor|node_modules|dist)/", e.get("path", ""))
            and not LOW_SIGNAL_PATH.search(e.get("path", ""))
        ]

        if not paths:
            if debug:
                print(f"[{full_name}] no matching files found")
            return []

        # --- LLM file selection (I/O) ---
        t1 = time.time()
        selected = await select_relevant_files(llm_call_fn, query, paths, max_files, readme_excerpt)
        if debug:
            print(f"[{full_name}] file select: {selected} ({time.time()-t1:.2f}s)")

        # --- Fetch file contents (I/O, parallel across files) ---
        raw_contents = await asyncio.gather(
            *[fetch_raw_file(session, full_name, default_branch, p) for p in selected],
            return_exceptions=True
        )
        contents = [c if isinstance(c, str) else "" for c in raw_contents]

        # --- Chunk each file: clean (CPU) → AST (CPU) → LLM refine (I/O) ---
        # Run all file chunking concurrently within this repo
        async def _chunk_file(fpath, fcontent):
            t2 = time.time()
            chunks = await hybrid_chunk(llm_call_fn, query, fcontent, full_name, fpath, debug)
            for c in chunks:
                c.stars = stars
            if debug:
                print(f"  [{full_name}/{fpath}] chunked in {time.time()-t2:.2f}s → {len(chunks)} chunks")
            return chunks

        chunk_tasks = [
            _chunk_file(fpath, fcontent)
            for fpath, fcontent in zip(selected, contents)
            if fcontent
        ]
        chunk_results = await asyncio.gather(*chunk_tasks, return_exceptions=True)
        all_chunks = []
        for result in chunk_results:
            if isinstance(result, list):
                all_chunks.extend(result)

        # Per-repo cap (applied AFTER hybrid chunking, which already filtered)
        if len(all_chunks) > chunk_cap:
            all_chunks = all_chunks[:chunk_cap]

        if debug:
            print(f"[{full_name}] DONE: {len(all_chunks)} chunks, {time.time()-t0:.2f}s total")

        return all_chunks

    except Exception as e:
        if debug:
            print(f"[{full_name}] FAILED: {e}")
        return []


# ============================================================================
# 15. ORCHESTRATION — the main tool function with streaming embedder
# ============================================================================

async def research_github(
    query: str,
    llm_call_fn,
    keywords: Optional[list] = None,
    language: Optional[str] = None,
    min_stars: int = 5,
    max_stars: int = 2000,
    max_repos: int = 6,
    top_chunks: int = 8,
    propositions: Optional[list] = None,
    expansion: Optional[SearchExpansion] = None,
    debug: bool = False,
) -> dict:
    """Core retrieval pipeline. debug=True prints stage-by-stage timing.
    Accepts optional propositions from extract_deep_propositions() for multi-hop search."""
    keywords = keywords or []
    MAX_FILES_PER_REPO = 3
    MAX_TOTAL_CHUNKS = 40
    per_repo_cap = max(1, MAX_TOTAL_CHUNKS // max_repos)
    t_start = time.time()

    connector = aiohttp.TCPConnector(
        limit=30,
        limit_per_host=10,
        ttl_dns_cache=300,
        keepalive_timeout=30,
    )
    async with aiohttp.ClientSession(connector=connector) as session:

        # ── Phase 1: Discovery (parallel I/O) ──────────────────────────
        t_expand = time.time()
        if expansion is None:
            expansion = await expand_search_query(llm_call_fn, query, keywords, language)

        # Eliminate term flatness: merge sharp proposition terms directly into discovery search expansion
        if propositions and expansion:
            prop_terms = []
            for p in propositions:
                if isinstance(p, SearchProposition) and p.code_terms:
                    prop_terms.extend(p.code_terms)
            if prop_terms:
                seen_t = set()
                combined = []
                for t in prop_terms + expansion.code_terms:
                    if t.lower() not in seen_t:
                        seen_t.add(t.lower())
                        combined.append(t)
                expansion.code_terms = combined[:6]

        if debug:
            print(f"[expand_search] {time.time()-t_expand:.2f}s | "
                  f"CODE_TERMS={expansion.code_terms} TOPICS={expansion.topics} "
                  f"EXCLUDE={expansion.exclude_topics} EXT={expansion.extra_extensions}")

        # Build extension pattern based on LLM expansion
        ext_pattern = BASE_EXTENSIONS
        if expansion.extra_extensions:
            extra = "|".join(e.lstrip(".") for e in expansion.extra_extensions)
            ext_pattern = rf"\.({BASE_EXTENSIONS.strip(r'\.$()').strip(r'\.')}|{extra})$"
            # Rebuild cleanly
            base_exts = "py|js|ts|go|java|rs"
            ext_pattern = rf"\.({base_exts}|{extra})$"

        repos = await discover_repos(
            session, query, keywords, language, min_stars, max_stars,
            max_repos, expansion, propositions=propositions, debug=debug,
        )

        if not repos:
            return {
                "query": query, "chunks": [],
                "retrieval_confidence": 0.0,
                "retrieval_signal": "EMPTY",
                "suggested_fallback": "SEMANTIC",
                "note": "no repos found matching the query",
                "timings": {"total": round(time.time() - t_start, 2)},
            }

        # ── Phase 2: Per-repo pipelines (parallel) + streaming embed ───
        # Multi-query expansion runs concurrently with repo pipelines
        query_expansion_task = asyncio.create_task(
            expand_query_for_retrieval(llm_call_fn, query, keywords)
        )

        # Streaming embedder: embed chunks as each repo finishes
        embed_queue = asyncio.Queue()
        embedded_chunks = []

        async def streaming_embedder():
            batch = []
            while True:
                try:
                    item = await asyncio.wait_for(embed_queue.get(), timeout=3.0)
                    if item is None:  # sentinel
                        break
                    batch.append(item)
                    if len(batch) >= EMBED_BATCH_SIZE:
                        try:
                            vecs = await _embed_batch_cached(
                                session, [c.content for c in batch], "passage"
                            )
                            for c, v in zip(batch, vecs):
                                c.embedding = v
                            embedded_chunks.extend(batch)
                        except Exception as embed_err:
                            if debug:
                                print(f"[embed] batch failed: {embed_err}")
                        batch = []
                except asyncio.TimeoutError:
                    # Flush what we have
                    if batch:
                        try:
                            vecs = await _embed_batch_cached(
                                session, [c.content for c in batch], "passage"
                            )
                            for c, v in zip(batch, vecs):
                                c.embedding = v
                            embedded_chunks.extend(batch)
                        except Exception as embed_err:
                            if debug:
                                print(f"[embed] flush failed: {embed_err}")
                        batch = []
            # Final flush
            if batch:
                try:
                    vecs = await _embed_batch_cached(
                        session, [c.content for c in batch], "passage"
                    )
                    for c, v in zip(batch, vecs):
                        c.embedding = v
                    embedded_chunks.extend(batch)
                except Exception as embed_err:
                    if debug:
                        print(f"[embed] final flush failed: {embed_err}")

        embedder_task = asyncio.create_task(streaming_embedder())

        # Run all repo pipelines concurrently
        async def repo_pipeline_with_emit(repo):
            chunks = await process_repo(
                session, repo, query, llm_call_fn,
                ext_pattern, MAX_FILES_PER_REPO, per_repo_cap, debug,
            )
            return chunks

        t_repos = time.time()
        repo_results = await asyncio.gather(
            *[repo_pipeline_with_emit(repo) for repo in repos],
            return_exceptions=True,
        )

        # Collect chunks, log failures
        all_raw_chunks = []
        for repo, result in zip(repos, repo_results):
            if isinstance(result, Exception):
                if debug:
                    print(f"[{repo.get('full_name', '?')}] FAILED: {result}")
                continue
            all_raw_chunks.extend(result)

        if debug:
            print(f"[repo pipelines] {time.time()-t_repos:.2f}s | "
                  f"{len(all_raw_chunks)} raw chunks from {len(repos)} repos")

        # Deduplicate BEFORE embedding (saves API calls)
        t_dedup = time.time()
        deduped_chunks = deduplicate_chunks(all_raw_chunks)
        if debug:
            print(f"[dedup] {time.time()-t_dedup:.2f}s | "
                  f"{len(all_raw_chunks)} → {len(deduped_chunks)} unique chunks")

        # Feed deduped chunks to streaming embedder
        for c in deduped_chunks:
            await embed_queue.put(c)
        await embed_queue.put(None)  # sentinel
        await embedder_task  # wait for all embeddings to complete

        if not embedded_chunks:
            return {
                "query": query, "chunks": [],
                "note": "no candidate chunks after processing",
                "timings": {"total": round(time.time() - t_start, 2)},
            }

        # ── Phase 3: Global convergence ────────────────────────────────

        # Get query variations (should be done by now, was running in parallel)
        query_variations = await query_expansion_task
        if debug:
            print(f"[multi-query] variations: {query_variations}")

        # Embed query variations
        t_qembed = time.time()
        query_vecs = await embed_texts(session, query_variations, input_type="query")
        if debug:
            print(f"[query embed] {time.time()-t_qembed:.2f}s | "
                  f"{len(query_variations)} variations embedded")

        # RRF Rerank (CPU — pure computation, zero I/O)
        t_rerank = time.time()
        judge_pool_size = max(top_chunks * 2, 15)
        candidates = rerank_rrf(embedded_chunks, query_vecs, query, judge_pool_size)
        if debug:
            print(f"[rerank_rrf] {time.time()-t_rerank:.2f}s | "
                  f"{len(embedded_chunks)} → {len(candidates)} candidates")

        # Final judge: Extract & Stitch LLM reranker
        t_judge = time.time()
        top, judge_fallback = await judge_chunks(
            llm_call_fn, query, candidates, top_chunks, session=session
        )
        if debug:
            print(f"[judge] {time.time()-t_judge:.2f}s | "
                  f"{len(candidates)} → {len(top)} final, fallback={judge_fallback}")

        # Path dedup on final output
        top = path_dedup_topn(top)

        # ── Confidence Scoring ─────────────────────────────────────────
        # Heuristic: no LLM call, pure computation from pipeline signals
        unique_repos = len(set(c.repo for c in top))
        judge_kept_ratio = len(top) / max(len(candidates), 1)
        top_score = max((c.score for c in top), default=0)

        if not top:
            confidence, signal, fallback = 0.0, "EMPTY", "SEMANTIC"
        elif judge_fallback and unique_repos <= 1:
            confidence, signal, fallback = 0.2, "LOW", "SEMANTIC"
        elif judge_fallback or unique_repos <= 1 or judge_kept_ratio < 0.3:
            confidence, signal, fallback = 0.5, "MEDIUM", None
        elif unique_repos >= 3 and judge_kept_ratio >= 0.5:
            confidence, signal, fallback = 0.9, "HIGH", None
        else:
            confidence, signal, fallback = 0.7, "MEDIUM", None

        if debug:
            print(f"[confidence] {signal} ({confidence:.1f}) | "
                  f"unique_repos={unique_repos}, judge_kept={judge_kept_ratio:.0%}, "
                  f"top_score={top_score:.4f}")
            print(f"[TOTAL] {time.time()-t_start:.2f}s end-to-end")

        return {
            "query": query,
            "query_variations": query_variations,
            "retrieval_confidence": round(confidence, 2),
            "retrieval_signal": signal,
            "suggested_fallback": fallback,
            "judge_used_fallback": judge_fallback,
            "timings": {
                "total": round(time.time() - t_start, 2),
            },
            "chunks": [
                {
                    "repo": c.repo,
                    "path": c.path,
                    "content": c.content,
                    "score": round(c.score, 4),
                }
                for c in top
            ],
        }

# ============================================================================
# 17. AGENT-FACING TOOL — code_retriever_tool()
# ============================================================================

async def code_retriever_tool(
    query: str,
    keywords: Optional[list] = None,
    language: Optional[str] = None,
    deep_search: bool = True,
    min_stars: int = 5,
    max_stars: int = 2000,
    max_repos: int = 6,
    top_chunks: int = 8,
    debug: bool = False,
) -> dict:
    """Agent-facing tool interface with adaptive gating + deep proposition extraction.

    Speculative Parallel Execution: fires gate, propositions, and expansion
    ALL concurrently. If gate says SKIP, the other results are discarded.
    Saves ~4-6s vs serial execution.

    Uses its own Keys 1+2 internally — completely independent from the Agent's Key 3+4.
    """
    keywords = keywords or []
    t_start = time.time()

    # ── 0. Persistent WAL Query Result Cache Check (< 1 ms) ────────────
    cache_raw = f"{query.strip().lower()}|{language or ''}|{min_stars}|{max_stars}|{top_chunks}"
    cache_key = hashlib.sha256(cache_raw.encode()).hexdigest()
    cached_res = _db_get_result(cache_key, ttl=900)
    if cached_res:
        cached_res["cached_hit"] = True
        cached_res["timings"]["total"] = round(time.time() - t_start, 4)
        if debug:
            print(f"[cache_hit] returned persistent SQLite result in {time.time()-t_start:.4f}s")
        return cached_res

    # ── Speculative Parallel Launch ─────────────────────────────────────
    if debug:
        print("[speculative] launching gate + propositions + expansion in parallel...")

    tasks = [gate_code_retrieval(real_llm_call, query, keywords)]
    if deep_search:
        tasks.append(extract_deep_propositions(real_llm_call, query, keywords))
    else:
        async def _dummy_prop(): return None
        tasks.append(_dummy_prop())
    tasks.append(expand_search_query(real_llm_call, query, keywords, language or ""))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    gate = results[0] if isinstance(results[0], dict) else {
        "should_retrieve": True, "suggested_mode": "CODE", "reason": "gate failed, defaulting to retrieval"
    }
    propositions = results[1] if deep_search and isinstance(results[1], list) else None
    expansion = results[2] if isinstance(results[2], SearchExpansion) else None

    if debug:
        print(f"[speculative] completed in {time.time()-t_start:.2f}s | "
              f"gate={gate['suggested_mode']}, props={len(propositions) if propositions else 0}")

    # ── Gate Check ──────────────────────────────────────────────────────
    if not gate["should_retrieve"]:
        return {
            "query": query,
            "retrieval_confidence": 0.0,
            "retrieval_signal": "SKIP",
            "suggested_fallback": gate["suggested_mode"],
            "reason": gate["reason"],
            "chunks": [],
            "timings": {"total": round(time.time() - t_start, 2)},
        }

    # ── Full Retrieval Pipeline (with pre-computed expansion) ──────────
    result = await research_github(
        query=query,
        llm_call_fn=real_llm_call,
        keywords=keywords,
        language=language,
        min_stars=min_stars,
        max_stars=max_stars,
        max_repos=max_repos,
        top_chunks=top_chunks,
        propositions=propositions,
        expansion=expansion,
        debug=debug,
    )

    # Add gating metadata to result
    result["gate_mode"] = gate["suggested_mode"]
    result["gate_reason"] = gate["reason"]
    if propositions:
        result["propositions_used"] = [
            {"proposition": p.proposition, "code_terms": p.code_terms}
            for p in propositions
        ]

    # Store in persistent query-level WAL cache
    _db_set_result(cache_key, result)

    return result


# ============================================================================
# 18. EXAMPLE RUN
# ============================================================================

async def main():
    result = await code_retriever_tool(
        query="Linux shell hacking",
        keywords=["linux", "shell", "hacking"],
        language="python",
        deep_search=True,
        debug=True,
    )
    print("\n" + "=" * 60)
    print(f"Query: {result['query']}")
    print(f"Signal: {result.get('retrieval_signal')} "
          f"(confidence: {result.get('retrieval_confidence')})")
    print(f"Gate: {result.get('gate_mode')} — {result.get('gate_reason')}")
    if result.get('propositions_used'):
        print(f"Propositions: {len(result['propositions_used'])}")
        for p in result['propositions_used']:
            print(f"  → {p['proposition']} | {p['code_terms']}")
    print(f"Fallback: {result.get('suggested_fallback')}")
    print(f"Total time: {result['timings']['total']}s")
    print(f"Chunks returned: {len(result['chunks'])}")
    print("=" * 60)
    for ch in result["chunks"]:
        print(f"\n[{ch['score']:.4f}] {ch['repo']} / {ch['path']}")
        print(ch["content"][:200])
        print("---")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except RuntimeError:
        import nest_asyncio
        nest_asyncio.apply()
        asyncio.get_event_loop().run_until_complete(main())

# In Colab:
#   result = await code_retriever_tool(
#       query="Linux shell hacking",
#       keywords=["linux", "shell", "hacking"],
#       language="python",
#       deep_search=True,
#       debug=True,
#   )
