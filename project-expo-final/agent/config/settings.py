"""
All env vars, API keys, model names, and endpoint URLs — centralized.

Every tunable number lives here or in budgets.py. No magic strings
scattered across the codebase. Read at instantiation time (not import
time) so tests can override via monkeypatch / os.environ manipulation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv
load_dotenv()


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _env_list(prefix: str, max_keys: int = 10, fallback_key: str = "") -> list[str]:
    """Collect PREFIX_1, PREFIX_2, ... into a list.
    Falls back to fallback_key or the bare prefix if the numbered pattern isn't set."""
    keys = []
    for i in range(1, max_keys + 1):
        k = os.environ.get(f"{prefix}_{i}", "")
        if k:
            keys.append(k)
    if not keys and fallback_key:
        single = os.environ.get(fallback_key, "")
        if single:
            keys.append(single)
    if not keys:
        single = os.environ.get(prefix, "")
        if single:
            keys.append(single)
    return keys


@dataclass
class NIMSettings:
    """NVIDIA NIM endpoint config. Multi-key for throughput scaling."""

    base_url: str = field(default_factory=lambda: _env(
        "NIM_BASE_URL", "https://integrate.api.nvidia.com/v1"))
        
    groq_api_key: str = field(default_factory=lambda: _env("GROQ_API_KEY", ""))
    groq_api_keys: list[str] = field(default_factory=lambda: _env_list("GROQ_API_KEY"))
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_worker_model: str = field(default_factory=lambda: _env("GROQ_WORKER_MODEL", "llama-3.1-8b-instant"))

    # Models — user can override via env
    chat_model: str = field(default_factory=lambda: _env(
        "NIM_CHAT_MODEL", "nvidia/llama-3.1-nemotron-ultra-253b-v1"))
    fast_model: str = field(default_factory=lambda: _env(
        "NIM_FAST_MODEL", "nvidia/llama-3.1-nemotron-ultra-253b-v1"))
    embed_model: str = field(default_factory=lambda: _env(
        "NIM_EMBED_MODEL", "nvidia/nv-embedqa-e5-v5"))

    # Multi-key round-robin — 2 keys from separate accounts = 2× throughput
    api_keys: list[str] = field(default_factory=lambda: _env_list("NVIDIA_API_KEY", fallback_key="NIM_API_KEY"))
    
    # Partitioned Dedicated Pools
    agent_keys: list[str] = field(default_factory=lambda: _env_list("AGENT_NIM_KEY"))
    code_keys: list[str] = field(default_factory=lambda: _env_list("CODE_NIM_KEY"))

    # Per-key concurrency semaphore slots
    per_key_concurrency: int = 5

    # Timeouts (seconds)
    chat_timeout: float = field(default_factory=lambda: float(_env("NIM_CHAT_TIMEOUT", "30.0")))
    fast_timeout: float = field(default_factory=lambda: float(_env("NIM_FAST_TIMEOUT", "5.0")))
    embed_timeout: float = field(default_factory=lambda: float(_env("NIM_EMBED_TIMEOUT", "20.0")))
    stream_timeout: float = field(default_factory=lambda: float(_env("NIM_STREAM_TIMEOUT", "60.0")))

    # Retry config
    max_retries: int = 3
    backoff_base: float = 1.0
    backoff_max: float = 8.0

    @property
    def primary_key(self) -> str:
        return self.api_keys[0] if self.api_keys else ""


@dataclass
class BraveSettings:
    """Brave Search API config."""

    api_key: str = field(default_factory=lambda: _env("BRAVE_API_KEY", ""))
    base_url: str = "https://api.search.brave.com/res/v1/web/search"
    timeout: float = 8.0
    max_results: int = 8


@dataclass
class SearchSettings:
    """DuckDuckGo fallback when Brave isn't configured."""

    ddg_url: str = "https://lite.duckduckgo.com/lite/"
    timeout: float = 10.0
    max_results: int = 8


@dataclass
class SerpAPISettings:
    """SerpAPI fallback — Google search when Brave key absent."""

    api_key: str = field(default_factory=lambda: _env("SERPAPI_KEY", ""))
    api_keys: list[str] = field(default_factory=lambda: _env_list("SERPAPI_KEY"))
    base_url: str = "https://serpapi.com/search"
    engine: str = "google"
    timeout: float = 10.0


@dataclass
class GitHubSettings:
    """GitHub API config for the code retriever block."""

    token: str = field(default_factory=lambda: _env("GITHUB_TOKEN", ""))
    api_base: str = "https://api.github.com"
    timeout: float = 10.0
    max_concurrent: int = 10


@dataclass
class DriveSettings:
    """Google Drive OAuth2 config."""

    client_id: str = field(default_factory=lambda: _env("GOOGLE_CLIENT_ID", ""))
    client_secret: str = field(default_factory=lambda: _env("GOOGLE_CLIENT_SECRET", ""))
    redirect_uri: str = field(default_factory=lambda: _env(
        "GOOGLE_REDIRECT_URI", "http://localhost:8000/auth/google/callback"))


@dataclass
class Settings:
    """Top-level settings container. Single source of truth."""

    nim: NIMSettings = field(default_factory=NIMSettings)
    brave: BraveSettings = field(default_factory=BraveSettings)
    search: SearchSettings = field(default_factory=SearchSettings)
    serpapi: SerpAPISettings = field(default_factory=SerpAPISettings)
    github: GitHubSettings = field(default_factory=GitHubSettings)
    drive: DriveSettings = field(default_factory=DriveSettings)

    # Server
    host: str = field(default_factory=lambda: _env("AGENT_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(_env("AGENT_PORT", "8000")))
    debug: bool = field(default_factory=lambda: _env("AGENT_DEBUG", "").lower() in ("1", "true", "yes"))

    # Cache
    cache_dir: str = field(default_factory=lambda: _env(
        "AGENT_CACHE_DIR", os.path.join(os.path.expanduser("~"), ".agent_cache")))
    llm_cache_max: int = 500            # max in-memory LRU entries
    llm_cache_ttl: int = 3600           # 1 hour
    embed_cache_ttl: int = 86400        # 24 hours
    semantic_cache_threshold: float = 0.95
    semantic_cache_ttl: int = 900       # 15 minutes

    # KB
    kb_dir: str = field(default_factory=lambda: _env(
        "AGENT_KB_DIR", os.path.join(os.path.expanduser("~"), ".agent_kb")))


# Module-level singleton — import this everywhere
settings = Settings()
