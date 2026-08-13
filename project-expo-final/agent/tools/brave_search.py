"""
Brave Search API wrapper — FULLY utilizing all Brave capabilities.

Brave provides much more than basic web search. This module uses:
  1. Web Search with extra_snippets=true (5 additional excerpts per result)
  2. LLM Context endpoint (/res/v1/llm/context) — pre-extracted, token-efficient
     content specifically optimized for LLM consumption
  3. Image Search — for multimodal queries
  4. DuckDuckGo fallback when Brave isn't configured

The LLM Context endpoint is the KEY differentiator: it returns pre-extracted,
relevance-ranked text/tables/code directly from pages, eliminating the need
to scrape and clean HTML ourselves. This saves ~2-5s per query and produces
cleaner chunks than any scraper.
"""

from __future__ import annotations

import re
import logging
import asyncio
import random
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

from ..config.settings import settings
from ..config.budgets import MAX_SEARCH_RESULTS

logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    """One search result with URL, title, snippet, and optional extras."""
    url: str
    title: str
    snippet: str
    extra_snippets: list[str] = field(default_factory=list)
    # If True, snippet is rich enough to skip full page fetch
    snippet_sufficient: bool = False


@dataclass
class LLMContextResult:
    """Pre-extracted content from Brave's LLM Context endpoint."""
    url: str
    title: str
    text: str           # pre-extracted, clean text optimized for LLM
    # This is already chunked and cleaned by Brave — no scraping needed


@dataclass
class ImageResult:
    """One image search result."""
    url: str             # page URL
    image_url: str       # direct image URL
    title: str
    description: str


@dataclass
class BraveResults:
    """Combined results from all Brave endpoints."""
    web_results: list[SearchResult] = field(default_factory=list)
    context_results: list[LLMContextResult] = field(default_factory=list)
    image_results: list[ImageResult] = field(default_factory=list)


# ── Main entry point ──

async def brave_search(
    query: str,
    *,
    max_results: int = MAX_SEARCH_RESULTS,
    include_images: bool = False,
    use_llm_context: bool = True,
    session: Optional[aiohttp.ClientSession] = None,
) -> BraveResults:
    """Full parallel web search utilizing both Brave and SerpAPI simultaneously.
    If both fail or are missing keys, falls back to DuckDuckGo."""
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()

    try:
        tasks = []

        # ── Branch 1: Brave ──
        if settings.brave.api_key:
            async def _do_brave():
                b_tasks = [
                    _brave_web_search(query, max_results, session),
                    _brave_llm_context(query, session) if use_llm_context else asyncio.sleep(0),
                    _brave_image_search(query, session) if include_images else asyncio.sleep(0),
                ]
                b_res = await asyncio.gather(*b_tasks, return_exceptions=True)
                w = b_res[0] if isinstance(b_res[0], list) else []
                c = b_res[1] if isinstance(b_res[1], list) else []
                i = b_res[2] if isinstance(b_res[2], list) else []
                return BraveResults(web_results=w, context_results=c, image_results=i)
            
            tasks.append(_do_brave())

        # ── Branch 2: SerpAPI ──
        if settings.serpapi.api_key:
            async def _do_serp():
                web = await _serpapi_fallback(query, max_results, session)
                return BraveResults(web_results=web or [])
            tasks.append(_do_serp())

        # ── Execute Parallel ──
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            web_results = []
            context_results = []
            image_results = []
            
            for r in results:
                if isinstance(r, BraveResults):
                    web_results.extend(r.web_results)
                    context_results.extend(r.context_results)
                    image_results.extend(r.image_results)
                elif isinstance(r, Exception):
                    logger.warning("Parallel search branch failed: %s", r)
                    
            # Deduplicate by URL
            seen = set()
            unique_web = []
            for w in web_results:
                url = w.get("url")
                if url and url not in seen:
                    seen.add(url)
                    unique_web.append(w)
                    
            if unique_web or context_results:
                return BraveResults(
                    web_results=unique_web[:max_results],
                    context_results=context_results,
                    image_results=image_results
                )
                
            logger.info("Parallel APIs returned empty, trying DDG fallback")

        # ── Branch 3: DuckDuckGo Fallback ──
        web = await _ddg_fallback(query, max_results, session)
        return BraveResults(web_results=web)

    finally:
        if own_session and session:
            await session.close()


# ── Brave Web Search with extra_snippets ──

async def _brave_web_search(
    query: str,
    max_results: int,
    session: aiohttp.ClientSession,
) -> list[SearchResult]:
    """Web search with extra_snippets=true for 5 additional excerpts per result.
    Rich snippets can eliminate the need to fetch the full page."""
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": settings.brave.api_key,
    }
    params = {
        "q": query,
        "count": str(min(max_results, 20)),
        "text_decorations": "false",
        "extra_snippets": "true",  # KEY: 5 extra excerpts per result
    }

    try:
        async with session.get(
            settings.brave.base_url,
            headers=headers,
            params=params,
            timeout=aiohttp.ClientTimeout(total=settings.brave.timeout),
        ) as resp:
            if resp.status != 200:
                logger.warning("Brave web search returned %d", resp.status)
                return []

            data = await resp.json()
            results = []

            for item in data.get("web", {}).get("results", []):
                url = item.get("url", "")
                title = item.get("title", "")
                snippet = item.get("description", "")
                extra = item.get("extra_snippets", [])

                if not url:
                    continue

                # Combine snippet + extras for richness assessment
                all_text = snippet + " " + " ".join(extra)
                # Rich enough to skip full fetch? >300 chars combined is a good signal
                is_rich = len(all_text.strip()) > 300

                results.append(SearchResult(
                    url=url,
                    title=title,
                    snippet=snippet[:500],
                    extra_snippets=extra[:5],
                    snippet_sufficient=is_rich,
                ))

            return results[:max_results]

    except Exception as e:
        logger.warning("Brave web search error: %s", e)
        return []


# ── Brave LLM Context Endpoint ──

async def _brave_llm_context(
    query: str,
    session: aiohttp.ClientSession,
) -> list[LLMContextResult]:
    """Brave's LLM Context API — returns pre-extracted, relevance-ranked content
    specifically optimized for LLM consumption. This is the premium endpoint
    that eliminates the need for scraping and HTML cleaning.

    Returns clean text/tables/code blocks directly usable as chunks."""
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": settings.brave.api_key,
    }
    params = {"q": query}

    try:
        async with session.get(
            "https://api.search.brave.com/res/v1/llm/context",
            headers=headers,
            params=params,
            timeout=aiohttp.ClientTimeout(total=settings.brave.timeout),
        ) as resp:
            if resp.status != 200:
                # LLM Context may not be available on all plans
                logger.debug("Brave LLM Context returned %d (may not be on plan)", resp.status)
                return []

            data = await resp.json()
            results = []

            # The response format has "results" with pre-extracted content
            for item in data.get("results", data.get("web", {}).get("results", [])):
                url = item.get("url", "")
                title = item.get("title", "")
                # LLM Context returns pre-extracted text
                text = item.get("text", item.get("description", ""))
                extra = item.get("extra_snippets", [])
                if extra:
                    text = text + "\n\n" + "\n\n".join(extra)

                if url and text:
                    results.append(LLMContextResult(
                        url=url,
                        title=title,
                        text=text,
                    ))

            return results

    except Exception as e:
        logger.debug("Brave LLM Context error: %s", e)
        return []


# ── Brave Image Search ──

async def _brave_image_search(
    query: str,
    session: aiohttp.ClientSession,
    max_results: int = 5,
) -> list[ImageResult]:
    """Image search — for queries that need visual results (diagrams,
    architectures, UI examples, etc.)."""
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": settings.brave.api_key,
    }
    params = {
        "q": query,
        "count": str(min(max_results, 10)),
    }

    try:
        async with session.get(
            "https://api.search.brave.com/res/v1/images/search",
            headers=headers,
            params=params,
            timeout=aiohttp.ClientTimeout(total=settings.brave.timeout),
        ) as resp:
            if resp.status != 200:
                return []

            data = await resp.json()
            results = []

            for item in data.get("results", []):
                results.append(ImageResult(
                    url=item.get("url", ""),
                    image_url=item.get("properties", {}).get("url", item.get("thumbnail", {}).get("src", "")),
                    title=item.get("title", ""),
                    description=item.get("description", ""),
                ))

            return results[:max_results]

    except Exception as e:
        logger.debug("Brave image search error: %s", e)
        return []

# ── SerpAPI Fallback (Google Search) ──

async def _serpapi_fallback(
    query: str,
    max_results: int,
    session: Optional[aiohttp.ClientSession],
) -> list[SearchResult]:
    """SerpAPI Google search — structured JSON results, no scraping needed.

    Endpoint: https://serpapi.com/search?engine=google
    Returns organic_results with title, link, snippet.
    """
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()

    try:
        # Pick random key from list, fallback to single key
        active_key = (
            random.choice(settings.serpapi.api_keys)
            if settings.serpapi.api_keys else settings.serpapi.api_key
        )
        
        params = {
            "engine": settings.serpapi.engine,
            "q": query,
            "api_key": active_key,
            "num": str(min(max_results, 10)),
            "hl": "en",
            "gl": "us",
        }

        async with session.get(
            settings.serpapi.base_url,
            params=params,
            timeout=aiohttp.ClientTimeout(total=settings.serpapi.timeout),
        ) as resp:
            if resp.status != 200:
                logger.warning("SerpAPI returned %d", resp.status)
                return []

            data = await resp.json()
            results = []

            # Parse organic results
            for item in data.get("organic_results", []):
                if len(results) >= max_results:
                    break
                snippet = item.get("snippet", "")
                # SerpAPI also gives rich snippets and highlighted words
                highlighted = item.get("snippet_highlighted_words", [])
                results.append(SearchResult(
                    url=item.get("link", ""),
                    title=item.get("title", ""),
                    snippet=snippet,
                    extra_snippets=highlighted[:3] if highlighted else [],
                    snippet_sufficient=len(snippet) > 200,
                ))

            # Also check for answer_box (direct answers)
            answer_box = data.get("answer_box", {})
            if answer_box and answer_box.get("answer"):
                results.insert(0, SearchResult(
                    url=answer_box.get("link", ""),
                    title=answer_box.get("title", "Direct Answer"),
                    snippet=answer_box.get("answer", ""),
                    snippet_sufficient=True,
                ))

            # Knowledge graph if present
            kg = data.get("knowledge_graph", {})
            if kg and kg.get("description"):
                results.insert(0, SearchResult(
                    url=kg.get("source", {}).get("link", ""),
                    title=kg.get("title", ""),
                    snippet=kg.get("description", ""),
                    snippet_sufficient=True,
                ))

            return results

    except Exception as e:
        logger.warning("SerpAPI search failed: %s", e)
        return []
    finally:
        if own_session and session:
            await session.close()


# ── DuckDuckGo Fallback ──

async def _ddg_fallback(
    query: str,
    max_results: int,
    session: Optional[aiohttp.ClientSession],
) -> list[SearchResult]:
    """DuckDuckGo lite — same approach as jarvis/searchTools.ts."""
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()

    try:
        url = f"{settings.search.ddg_url}?q={aiohttp.helpers.quote(query, safe='')}&kl=us-en"
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; AgentBot/1.0)",
            "Accept": "text/html",
        }

        async with session.get(
            url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=settings.search.timeout),
        ) as resp:
            if resp.status != 200:
                return []

            html = await resp.text()
            link_matches = re.findall(
                r'<a[^>]+href="([^"]*)"[^>]*>([^<]+)</a>', html, re.IGNORECASE
            )
            snippet_matches = re.findall(
                r'<td class="result-snippet"[^>]*>([\s\S]*?)</td>', html, re.IGNORECASE
            )

            results = []
            snippet_idx = 0
            for href, text in link_matches:
                if len(results) >= max_results:
                    break
                if not href.startswith("http") or "duckduckgo.com" in href:
                    continue
                clean_title = re.sub(r"<[^>]+>", "", text).strip()
                if not clean_title or len(clean_title) < 5:
                    continue
                snippet = ""
                if snippet_idx < len(snippet_matches):
                    snippet = re.sub(r"<[^>]+>", "", snippet_matches[snippet_idx]).strip()[:200]
                    snippet_idx += 1
                results.append(SearchResult(
                    url=href, title=clean_title, snippet=snippet,
                ))
            return results

    except Exception as e:
        logger.warning("DDG search failed: %s", e)
        return []
    finally:
        if own_session and session:
            await session.close()
