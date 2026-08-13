"""
Source resolution — the first stage of each recursive node.

PRIORITY ORDER for getting content (cheapest/best first):
  1. Brave LLM Context endpoint → pre-extracted, no scraping needed
  2. Brave web results with rich snippets → skip fetch
  3. Brave web results needing fetch → bounded concurrent fetch
  4. KB search (in KB mode) → merged with public results

This pipeline is designed to MINIMIZE network calls:
- LLM Context gives us clean text without ANY scraping
- extra_snippets gives us ~300+ chars without fetching the page
- Only fetch URLs that have neither LLM context nor rich snippets

Direct URL mode: skip search entirely, just fetch + chunk.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import aiohttp

from ...tools.brave_search import (
    brave_search, BraveResults, SearchResult, LLMContextResult,
)
from ...tools.web_fetch import fetch_url_with_seam
from ...config.budgets import MAX_SEARCH_RESULTS, MAX_FETCH_CONCURRENT
from .types import Chunk, BlockInput, Mode, FetchFn
from .chunk import chunk_text

logger = logging.getLogger(__name__)


async def resolve_sources(
    inp: BlockInput,
    *,
    session: Optional[aiohttp.ClientSession] = None,
    kb_search_fn=None,
) -> list[Chunk]:
    """
    Source resolution. Routes by mode:
      - Direct URL → skip search, fetch + chunk
      - PUBLIC → Brave (all endpoints) → chunk, skip fetch where possible
      - KB → KB search AND public search concurrently, merge
    """
    # Case 1: Direct URL — skip search entirely
    if inp.url:
        raw = await fetch_url_with_seam(inp.url, fetch_fn=inp.fetch_fn, session=session)
        if not raw:
            return []
        return chunk_text(raw, inp.url, is_html=True, raw_text_for_title=raw)

    assert inp.query is not None, "BlockInput must have query or url"

    if inp.mode == Mode.PUBLIC:
        return await _resolve_public(inp.query, inp.fetch_fn, session)

    # KB mode: concurrent KB + public
    return await _resolve_kb(inp.query, inp.fetch_fn, session, kb_search_fn)


async def _resolve_public(
    query: str,
    fetch_fn: Optional[FetchFn],
    session: Optional[aiohttp.ClientSession],
) -> list[Chunk]:
    """PUBLIC mode: Brave → chunk, using all 3 result types."""
    brave_results = await brave_search(query, session=session)
    return await _process_brave_results(brave_results, query, fetch_fn, session)


async def _process_brave_results(
    brave_results: BraveResults,
    query: str,
    fetch_fn: Optional[FetchFn],
    session: Optional[aiohttp.ClientSession],
) -> list[Chunk]:
    """Process all Brave result types into chunks.

    Priority (cheapest/best first):
    1. LLM Context results → already pre-extracted, just chunk
    2. Rich snippets (snippet_sufficient=True) → skip fetch
    3. Remaining URLs → bounded concurrent fetch
    """
    chunks: list[Chunk] = []

    # ── Priority 1: LLM Context results (pre-extracted, NO fetch needed) ──
    # This is the best source — Brave already extracted clean text
    for ctx in brave_results.context_results:
        context_chunks = chunk_text(ctx.text, ctx.url, is_html=False, raw_text_for_title=ctx.text)
        for c in context_chunks:
            c.title = ctx.title
        chunks.extend(context_chunks)

    # Track which URLs we already have content for
    covered_urls = {ctx.url for ctx in brave_results.context_results}

    # ── Priority 2: Rich snippets (skip fetch) ──
    urls_needing_fetch: list[SearchResult] = []

    for sr in brave_results.web_results:
        if sr.url in covered_urls:
            continue  # Already have LLM Context for this URL

        # Build combined snippet text
        all_snippet_text = sr.snippet
        if sr.extra_snippets:
            all_snippet_text += "\n\n" + "\n\n".join(sr.extra_snippets)

        if sr.snippet_sufficient or len(all_snippet_text.strip()) > 300:
            # Rich enough — add as chunk, SKIP the expensive fetch
            chunks.append(Chunk(
                text=f"{sr.title}\n\n{all_snippet_text}",
                source_url=sr.url,
                title=sr.title,
            ))
            covered_urls.add(sr.url)
        elif sr.snippet and len(sr.snippet) > 80:
            # Has some snippet — add it as a chunk AND queue for fetch
            chunks.append(Chunk(
                text=f"{sr.title}\n\n{sr.snippet}" if sr.title else sr.snippet,
                source_url=sr.url,
                title=sr.title,
            ))
            urls_needing_fetch.append(sr)
        else:
            # No useful snippet — must fetch
            urls_needing_fetch.append(sr)

    # ── Priority 3: Fetch remaining URLs (bounded concurrent) ──
    if urls_needing_fetch:
        fetched = await _fetch_and_chunk(urls_needing_fetch, fetch_fn, session)
        chunks.extend(fetched)

    # ── Image results → metadata chunks ──
    for img in brave_results.image_results:
        if img.description and img.title:
            chunks.append(Chunk(
                text=f"[Image] {img.title}: {img.description}\nImage URL: {img.image_url}",
                source_url=img.url,
                title=f"Image: {img.title}",
            ))

    return chunks


async def _resolve_kb(
    query: str,
    fetch_fn: Optional[FetchFn],
    session: Optional[aiohttp.ClientSession],
    kb_search_fn=None,
) -> list[Chunk]:
    """KB mode: run KB search and public search concurrently, merge."""
    tasks = [_resolve_public(query, fetch_fn, session)]

    if kb_search_fn is not None:
        tasks.append(_run_kb_search(kb_search_fn, query))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    merged: list[Chunk] = []
    for result in results:
        if isinstance(result, list):
            merged.extend(result)
        elif isinstance(result, Exception):
            logger.warning("Source resolution task failed: %s", result)

    return merged


async def _run_kb_search(kb_search_fn, query: str) -> list[Chunk]:
    """Call the KB search function and wrap results as Chunks."""
    try:
        kb_chunks = await kb_search_fn(query)
        return kb_chunks if isinstance(kb_chunks, list) else []
    except Exception as e:
        logger.warning("KB search failed: %s", e)
        return []


async def _fetch_and_chunk(
    search_results: list[SearchResult],
    fetch_fn: Optional[FetchFn],
    session: Optional[aiohttp.ClientSession],
) -> list[Chunk]:
    """Fetch URLs that DON'T have rich snippets or LLM Context data.
    Only called for the subset of results where fetch is actually needed."""
    chunks: list[Chunk] = []

    sem = asyncio.Semaphore(MAX_FETCH_CONCURRENT)

    async def _bounded_fetch(sr: SearchResult) -> tuple[str, SearchResult]:
        async with sem:
            raw = await fetch_url_with_seam(sr.url, fetch_fn=fetch_fn, session=session)
            return raw, sr

    fetch_tasks = [_bounded_fetch(sr) for sr in search_results[:MAX_SEARCH_RESULTS]]
    results = await asyncio.gather(*fetch_tasks, return_exceptions=True)

    for result in results:
        if isinstance(result, Exception):
            continue
        raw, sr = result
        if not raw or len(raw.strip()) < 50:
            continue
        fetched_chunks = chunk_text(raw, sr.url, is_html=True, raw_text_for_title=raw)
        chunks.extend(fetched_chunks)

    return chunks
