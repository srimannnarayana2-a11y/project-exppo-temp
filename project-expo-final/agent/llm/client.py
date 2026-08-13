"""
NVIDIA NIM client — the single LLM/embedding engine for the whole system.

Consolidates the best of github_researchtool.py's multi-key round-robin
and the research-agent skeleton's NIMClient into one production client:

  - Multi-key round-robin (N keys from N accounts = N× throughput)
  - Per-key asyncio.Semaphore (avoids 429s from one key blocking another)
  - Exponential backoff on transient errors (429, 500, 502, 503, 504)
  - Connection pooling via shared aiohttp.ClientSession
  - Streaming chat completions (SSE delta)
  - Batch embeddings with Matryoshka truncation + L2 re-normalization
  - Configurable timeouts per call type (fast decision vs deep synthesis)

IMPORTANT: The aiohttp.ClientSession is created lazily and MUST be closed
explicitly via close() or used as an async context manager. This is not
optional — leaked sessions leak TCP connections.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import random
import math
from typing import AsyncIterator, Optional

import aiohttp

from ..config.settings import settings
from ..config.budgets import (
    EMBED_DIM, EMBED_BATCH_SIZE,
    AIOHTTP_TOTAL_CONNECTIONS, AIOHTTP_PER_HOST,
    AIOHTTP_KEEPALIVE_S, AIOHTTP_DNS_CACHE_S,
)

logger = logging.getLogger(__name__)


class NIMClient:
    """
    Async NVIDIA NIM client with multi-key support, connection pooling,
    and exponential backoff.

    Usage:
        client = NIMClient()
        answer = await client.chat([{"role": "user", "content": "hello"}])
        vectors = await client.embed(["some text"])
        await client.close()
    """

    def __init__(self, nim_settings=None, pool: str = "agent"):
        self._cfg = nim_settings or settings.nim
        self._session: Optional[aiohttp.ClientSession] = None

        # Multi-key round-robin state based on selected pool
        if pool == "code" and self._cfg.code_keys:
            self._keys = list(self._cfg.code_keys)
        elif pool == "agent" and self._cfg.agent_keys:
            self._keys = list(self._cfg.agent_keys)
        else:
            self._keys = list(self._cfg.api_keys) if self._cfg.api_keys else [""]
            
        self._key_idx = 0
        self._key_lock = asyncio.Lock()

        # Per-key semaphores — prevents one key from hogging all slots
        self._key_semas: dict[str, asyncio.Semaphore] = {
            k: asyncio.Semaphore(self._cfg.per_key_concurrency)
            for k in self._keys
        }

    # ----- Session management ------------------------------------------------

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(
                limit=AIOHTTP_TOTAL_CONNECTIONS,
                limit_per_host=AIOHTTP_PER_HOST,
                ttl_dns_cache=AIOHTTP_DNS_CACHE_S,
                keepalive_timeout=AIOHTTP_KEEPALIVE_S,
            )
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    # ----- Key rotation ------------------------------------------------------

    async def _next_key(self) -> str:
        async with self._key_lock:
            key = self._keys[self._key_idx % len(self._keys)]
            self._key_idx += 1
            return key

    def _headers(self, api_key: str) -> dict:
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    # ----- Retry with exponential backoff ------------------------------------

    async def _call_with_backoff(self, fn, max_retries: int = None):
        """Call fn(), retry on transient HTTP errors with exponential backoff.
        fn must be an async callable that raises on failure."""
        retries = max_retries if max_retries is not None else self._cfg.max_retries
        last_exc = None

        for attempt in range(retries + 1):
            try:
                return await fn()
            except aiohttp.ClientResponseError as e:
                last_exc = e
                if e.status in (429, 500, 502, 503, 504) and attempt < retries:
                    wait = min(
                        self._cfg.backoff_base * (2 ** attempt),
                        self._cfg.backoff_max,
                    )
                    logger.warning(
                        "NIM %s on attempt %d, backing off %.1fs",
                        e.status, attempt + 1, wait,
                    )
                    await asyncio.sleep(wait)
                else:
                    raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_exc = e
                if attempt < retries:
                    wait = min(
                        self._cfg.backoff_base * (2 ** attempt),
                        self._cfg.backoff_max,
                    )
                    err_msg = "Timeout" if isinstance(e, asyncio.TimeoutError) else str(e)
                    logger.warning(
                        "NIM transient error on attempt %d: %s, backing off %.1fs",
                        attempt + 1, err_msg, wait,
                    )
                    await asyncio.sleep(wait)
                else:
                    logger.error("NIM fatal error after %d retries: %s", retries, type(e).__name__)
                    raise

        raise last_exc  # type: ignore[misc]

    # ----- Chat completion ---------------------------------------------------

    async def chat(
        self,
        messages: list[dict],
        *,
        model: str = "",
        temperature: float = 0.0,
        max_tokens: int = 1024,
        response_format_json: bool = False,
        timeout: float = 0,
    ) -> str:
        """Non-streaming chat completion. Returns the text of the first choice."""
        model = model or self._cfg.chat_model
        timeout = timeout or self._cfg.chat_timeout
        api_key = await self._next_key()
        sema = self._key_semas.get(api_key, asyncio.Semaphore(5))

        body: dict = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format_json:
            body["response_format"] = {"type": "json_object"}

        async def _do():
            session = self._ensure_session()
            async with sema:
                async with session.post(
                    f"{self._cfg.base_url}/chat/completions",
                    headers=self._headers(api_key),
                    json=body,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                    return data["choices"][0]["message"]["content"]

        return await self._call_with_backoff(_do)

    async def chat_fast(
        self,
        messages: list[dict],
        *,
        model: str = "",
        temperature: float = 0.0,
        max_tokens: int = 256,
        response_format_json: bool = False,
    ) -> str:
        """Fast decision call — shorter timeout, smaller token budget."""
        return await self.chat(
            messages,
            model=model or self._cfg.fast_model,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format_json=response_format_json,
            timeout=self._cfg.fast_timeout,
        )

    async def chat_worker(
        self,
        messages: list[dict],
        *,
        model: str = "",
        temperature: float = 0.0,
        max_tokens: int = 1024,
        response_format_json: bool = False,
    ) -> str:
        """Dedicated worker for non-thinking high-speed semantic tasks via Groq."""
        # Pick random key from list, fallback to single key
        active_key = (
            random.choice(self._cfg.groq_api_keys)
            if self._cfg.groq_api_keys else self._cfg.groq_api_key
        )
        if not active_key:
            return await self.chat_fast(
                messages, model=model, temperature=temperature,
                max_tokens=max_tokens, response_format_json=response_format_json
            )

        model = model or self._cfg.groq_worker_model
        body: dict = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format_json:
            body["response_format"] = {"type": "json_object"}

        async def _do():
            session = self._ensure_session()
            headers = {
                "Authorization": f"Bearer {active_key}",
                "Content-Type": "application/json",
            }
            async with session.post(
                f"{self._cfg.groq_base_url}/chat/completions",
                headers=headers,
                json=body,
                timeout=aiohttp.ClientTimeout(total=self._cfg.fast_timeout),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
                return data["choices"][0]["message"]["content"]

        return await self._call_with_backoff(_do)

    # ----- Streaming chat ----------------------------------------------------

    async def chat_stream(
        self,
        messages: list[dict],
        *,
        model: str = "",
        temperature: float = 0.0,
        max_tokens: int = 2048,
    ) -> AsyncIterator[str]:
        """Streaming chat completion — yields text deltas as they arrive.
        Used by synthesis.py so tokens reach the user immediately."""
        model = model or self._cfg.chat_model
        api_key = await self._next_key()
        sema = self._key_semas.get(api_key, asyncio.Semaphore(5))

        body = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }

        session = self._ensure_session()
        async with sema:
            async with session.post(
                f"{self._cfg.base_url}/chat/completions",
                headers=self._headers(api_key),
                json=body,
                timeout=aiohttp.ClientTimeout(total=self._cfg.stream_timeout),
            ) as resp:
                resp.raise_for_status()
                async for line in resp.content:
                    decoded = line.decode("utf-8").strip()
                    if not decoded or not decoded.startswith("data:"):
                        continue
                    payload = decoded[len("data:"):].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                        delta = chunk["choices"][0]["delta"].get("content")
                        if delta:
                            yield delta
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue

    # ----- Embeddings --------------------------------------------------------

    async def embed(
        self,
        texts: list[str],
        *,
        input_type: str = "passage",
        dim: int = EMBED_DIM,
    ) -> list[list[float]]:
        """Batch embedding with Matryoshka truncation + L2 re-normalization.
        Processes in batches of EMBED_BATCH_SIZE, returns one vector per text."""
        if not texts:
            return []

        all_vectors: list[list[float]] = []
        for i in range(0, len(texts), EMBED_BATCH_SIZE):
            batch = texts[i : i + EMBED_BATCH_SIZE]
            vecs = await self._embed_batch(batch, input_type)
            all_vectors.extend(vecs)

        # Matryoshka truncation + L2 re-normalize
        return [self._normalize_vec(v, dim) for v in all_vectors]

    async def _embed_batch(self, texts: list[str], input_type: str) -> list[list[float]]:
        api_key = await self._next_key()
        sema = self._key_semas.get(api_key, asyncio.Semaphore(5))

        body = {
            "model": self._cfg.embed_model,
            "input": texts,
            "input_type": input_type,
            "encoding_format": "float",
        }

        async def _do():
            session = self._ensure_session()
            async with sema:
                async with session.post(
                    f"{self._cfg.base_url}/embeddings",
                    headers=self._headers(api_key),
                    json=body,
                    timeout=aiohttp.ClientTimeout(total=self._cfg.embed_timeout),
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                    # Sort by index — API doesn't guarantee order
                    ordered = sorted(data["data"], key=lambda d: d["index"])
                    return [d["embedding"] for d in ordered]

        return await self._call_with_backoff(_do)

    @staticmethod
    def _normalize_vec(v: list[float], dim: int = EMBED_DIM) -> list[float]:
        """Truncate to dim + L2 re-normalize (Matryoshka requirement)."""
        sliced = v[:dim]
        norm = math.sqrt(sum(x * x for x in sliced)) or 1.0
        return [x / norm for x in sliced]

    # ----- NVIDIA Rerank API -------------------------------------------------

    async def rerank(
        self,
        query: str,
        passages: list[str],
        *,
        model: str = "nvidia/nv-rerankqa-mistral-4b-v3",
        top_n: int = 10,
    ) -> list[dict]:
        """Call NVIDIA's /v1/ranking endpoint for fast logit-based reranking.
        Returns list of {index, logit} sorted by logit descending."""
        api_key = await self._next_key()
        sema = self._key_semas.get(api_key, asyncio.Semaphore(5))

        body = {
            "model": model,
            "query": {"text": query},
            "passages": [{"text": p[:1000]} for p in passages],
        }

        async def _do():
            session = self._ensure_session()
            async with sema:
                async with session.post(
                    f"{self._cfg.base_url}/ranking",
                    headers=self._headers(api_key),
                    json=body,
                    timeout=aiohttp.ClientTimeout(total=5.0),
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                    rankings = data.get("rankings", data.get("data", []))
                    results = []
                    for r in rankings:
                        results.append({
                            "index": r.get("index", 0),
                            "logit": float(r.get("logit", r.get("score", 0.0))),
                        })
                    results.sort(key=lambda x: x["logit"], reverse=True)
                    return results[:top_n]

        try:
            return await self._call_with_backoff(_do, max_retries=1)
        except Exception:
            # Rerank API is optional — degrade gracefully
            logger.warning("NVIDIA rerank API failed, falling back to RRF only")
            return []


# Module-level singleton
_client: Optional[NIMClient] = None


def get_client() -> NIMClient:
    """Get or create the module-level NIMClient singleton."""
    global _client
    if _client is None:
        _client = NIMClient()
    return _client
