"""
Web fetch tool — stub with built-in aiohttp+regex fallback.

This is an INJECTABLE SEAM: the user can pass their own fetch_fn via
BlockInput.fetch_fn to use their custom scraper (e.g., the jarvis
WebFetchTool, Playwright, etc.). When no custom fn is provided, the
built-in fallback handles basic HTML fetching and text extraction.

The built-in is intentionally simple — it's a fallback, not a production
scraper. JavaScript-rendered pages won't work with this; that's what
the user's custom tool is for.
"""

from __future__ import annotations

import re
import logging
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

# Reuse the HTML stripping from chunk.py
from ..blocks.semantic.chunk import strip_html


async def fetch_url(
    url: str,
    *,
    max_chars: int = 50_000,
    timeout: float = 15.0,
    session: Optional[aiohttp.ClientSession] = None,
) -> str:
    """Fetch a URL and return cleaned text content.

    Built-in fallback — handles basic HTML/text pages. For JavaScript-
    rendered content or authenticated pages, inject a custom fetch_fn.
    """
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; AgentBot/1.0; +https://github.com)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8,*/*;q=0.5",
        }

        async with session.get(
            url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
            allow_redirects=True,
        ) as resp:
            if resp.status != 200:
                logger.warning("fetch_url got %d for %s", resp.status, url)
                return ""

            content_type = resp.headers.get("content-type", "")
            raw = await resp.text(errors="replace")

            # Strip HTML if content looks like HTML
            if "html" in content_type.lower() or raw.strip().startswith("<"):
                text = strip_html(raw)
            else:
                text = raw

            # Truncate
            if len(text) > max_chars:
                text = text[:max_chars] + "\n\n… [content truncated]"

            return text

    except Exception as e:
        logger.warning("fetch_url failed for %s: %s", url, e)
        return ""
    finally:
        if own_session and session:
            await session.close()


async def fetch_url_with_seam(
    url: str,
    *,
    fetch_fn=None,
    session: Optional[aiohttp.ClientSession] = None,
) -> str:
    """Use injected fetch_fn if provided, otherwise built-in fallback.

    This is the function blocks call — they pass BlockInput.fetch_fn
    through, and this routes to either the custom tool or built-in."""
    if fetch_fn is not None:
        try:
            return await fetch_fn(url)
        except Exception as e:
            logger.warning("Custom fetch_fn failed for %s: %s, using fallback", url, e)

    return await fetch_url(url, session=session)
