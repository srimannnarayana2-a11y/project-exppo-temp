"""
Every tunable concurrency limit, depth budget, and timeout in one place.
Separate from settings.py because these are algorithmic parameters, not
connection/credential config.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
DEFAULT_MAX_SUBAGENTS = 4        # normal cap on concurrent subagents
FAN_OUT_MAX_SUBAGENTS = 8        # cap for mechanical, provably-independent unit lists

# ---------------------------------------------------------------------------
# Recursive retriever block
# ---------------------------------------------------------------------------
DEFAULT_MAX_DEPTH = 3            # default recursion depth per query
MAX_EXTENSIONS_PER_BRANCH = 1    # one-shot dynamic depth extension
EXTENSION_INCREMENT = 1          # how much deeper one grant buys
NODE_TIMEOUT_S = 8.0             # per-node hard timeout
GLOBAL_BUDGET_S = 28.0           # whole tree must finish by this
MIN_TIME_MARGIN_S = 3.0          # required slack before global deadline to grant extension
INFO_GAIN_THRESHOLD = 0.3        # minimum delta to justify another recursion level

# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------
EMBED_DIM = 512                  # Matryoshka truncation dimension
EMBED_BATCH_SIZE = 16            # texts per API call
MAX_CHUNK_CHARS = 4000           # max chars per chunk before splitting
MIN_CHUNK_CHARS = 100            # chunks smaller than this get merged with neighbors
CHUNK_OVERLAP_RATIO = 0.1        # 10% overlap between consecutive chunks

# ---------------------------------------------------------------------------
# Reranking
# ---------------------------------------------------------------------------
RRF_K = 60                       # reciprocal rank fusion constant
TOP_K_RERANK = 15                # how many candidates to keep after RRF
TOP_K_FINAL = 8                  # how many to keep after judge/LLM rerank
RERANK_LOGIT_CUTOFF = -2.5       # NVIDIA rerank API logit threshold

# ---------------------------------------------------------------------------
# Code retriever (github_researchtool.py wrapper)
# ---------------------------------------------------------------------------
CODE_MAX_REPOS = 6
CODE_MAX_FILES_PER_REPO = 3
CODE_MAX_TOTAL_CHUNKS = 40
CODE_MIN_STARS = 5
CODE_MAX_STARS = 2000

# ---------------------------------------------------------------------------
# Pivot loop
# ---------------------------------------------------------------------------
MAX_PIVOT_ROUNDS = 2             # how many hypothesize→discriminate rounds before abandoning
CIRCUIT_BREAK_THRESHOLD = 3      # consecutive failures to trigger circuit break

# ---------------------------------------------------------------------------
# Critique
# ---------------------------------------------------------------------------
CRITIQUE_TEMPERATURE = 0.3       # diversity in persona responses

# ---------------------------------------------------------------------------
# Clarify
# ---------------------------------------------------------------------------
CLARIFY_TEMPERATURE = 0.2        # conservative for EIG scoring

# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
MAX_SEARCH_RESULTS = 8           # max URLs from Brave/DDG
MAX_FETCH_CONCURRENT = 5         # concurrent URL fetches per source resolution

# ---------------------------------------------------------------------------
# Connection pooling
# ---------------------------------------------------------------------------
AIOHTTP_TOTAL_CONNECTIONS = 30
AIOHTTP_PER_HOST = 10
AIOHTTP_KEEPALIVE_S = 30
AIOHTTP_DNS_CACHE_S = 300
