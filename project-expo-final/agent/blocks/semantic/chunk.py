"""
Text chunking — sentence-boundary-aware splitting for semantic retrieval.

Not the AST+LLM chunking from github_researchtool.py (that's for code) —
this is for web pages, articles, documentation. Design principles:

  - Sentence-boundary splits, not naive character cuts
  - Overlap between chunks for context preservation
  - Code fences preserved intact (never split mid-code-block)
  - Small docs stay as one chunk, large docs split at paragraph boundaries
  - Metadata attached: source_url, position index, parent title
"""

from __future__ import annotations

import re
from typing import Optional

from ...config.budgets import MAX_CHUNK_CHARS, MIN_CHUNK_CHARS, CHUNK_OVERLAP_RATIO
from .types import Chunk


# ---------------------------------------------------------------------------
# HTML / boilerplate stripping
# ---------------------------------------------------------------------------

_SCRIPT_STYLE = re.compile(r"<(script|style)[^>]*>[\s\S]*?</\1>", re.IGNORECASE)
_HTML_TAG = re.compile(r"<[^>]+>")
_HTML_ENTITIES = {
    "&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">",
    "&quot;": '"', "&#39;": "'",
}
_MULTI_NEWLINE = re.compile(r"\n{3,}")
_MULTI_SPACE = re.compile(r" {3,}")


def strip_html(text: str) -> str:
    """Convert HTML to readable text. Lightweight — not a full parser,
    but catches >95% of real web page content."""
    text = _SCRIPT_STYLE.sub("", text)
    text = _HTML_TAG.sub(" ", text)
    for entity, char in _HTML_ENTITIES.items():
        text = text.replace(entity, char)
    text = _MULTI_SPACE.sub(" ", text)
    text = _MULTI_NEWLINE.sub("\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Title extraction
# ---------------------------------------------------------------------------

_TITLE_TAG = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_H1_TAG = re.compile(r"<h1[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
_MD_H1 = re.compile(r"^#\s+(.+)$", re.MULTILINE)


def extract_title(raw_text: str) -> str:
    """Try to extract a title from HTML or Markdown."""
    for pattern in (_TITLE_TAG, _H1_TAG):
        m = pattern.search(raw_text)
        if m:
            return _HTML_TAG.sub("", m.group(1)).strip()[:200]
    m = _MD_H1.search(raw_text)
    if m:
        return m.group(1).strip()[:200]
    return ""


# ---------------------------------------------------------------------------
# Code fence detection
# ---------------------------------------------------------------------------

_CODE_FENCE = re.compile(r"```[\s\S]*?```", re.MULTILINE)


def _extract_code_fences(text: str) -> tuple[str, list[tuple[int, int, str]]]:
    """Replace code fences with placeholders, return (modified_text, fences).
    This prevents splitting mid-code-block."""
    fences = []
    offset = 0
    result = text

    for match in _CODE_FENCE.finditer(text):
        placeholder = f"\n__CODE_FENCE_{len(fences)}__\n"
        fences.append((match.start() - offset, match.end() - offset, match.group()))
        result = result[:match.start() - offset] + placeholder + result[match.end() - offset:]
        offset += len(match.group()) - len(placeholder)

    return result, fences


def _restore_code_fences(text: str, fences: list[tuple[int, int, str]]) -> str:
    """Restore code fence placeholders back to actual code."""
    for i, (_, _, code) in enumerate(fences):
        placeholder = f"__CODE_FENCE_{i}__"
        text = text.replace(placeholder, code)
    return text


# ---------------------------------------------------------------------------
# Sentence splitting
# ---------------------------------------------------------------------------

# Sentence boundary: period/question/exclamation followed by space+capital or newline
_SENT_BOUNDARY = re.compile(
    r"(?<=[.!?])\s+(?=[A-Z\n])"
    r"|(?<=\n)\n+"   # paragraph breaks
)


def _split_sentences(text: str) -> list[str]:
    """Split text into sentences, preserving paragraph structure."""
    parts = _SENT_BOUNDARY.split(text)
    return [p.strip() for p in parts if p.strip()]


# ---------------------------------------------------------------------------
# Main chunking function
# ---------------------------------------------------------------------------

def chunk_text(
    text: str,
    source_url: str,
    *,
    max_chars: int = MAX_CHUNK_CHARS,
    min_chars: int = MIN_CHUNK_CHARS,
    overlap_ratio: float = CHUNK_OVERLAP_RATIO,
    is_html: bool = False,
    raw_text_for_title: str = "",
) -> list[Chunk]:
    """Split text into semantically meaningful chunks.

    Args:
        text: The text to chunk (already cleaned if is_html=False)
        source_url: URL provenance for every chunk
        max_chars: Maximum characters per chunk
        min_chars: Merge chunks smaller than this with neighbors
        overlap_ratio: Fraction of overlap between consecutive chunks
        is_html: If True, strip HTML first
        raw_text_for_title: Original raw text for title extraction
    """
    if is_html:
        title = extract_title(raw_text_for_title or text)
        text = strip_html(text)
    else:
        title = extract_title(raw_text_for_title) if raw_text_for_title else ""

    text = text.strip()
    if not text:
        return []

    # Small document — one chunk
    if len(text) <= max_chars:
        return [Chunk(
            text=text,
            source_url=source_url,
            title=title,
            position=0,
        )]

    # Protect code fences from splitting
    text_safe, fences = _extract_code_fences(text)

    # Split into sentences
    sentences = _split_sentences(text_safe)
    if not sentences:
        # Fallback: hard split
        sentences = [text_safe[i:i + max_chars] for i in range(0, len(text_safe), max_chars)]

    # Group sentences into chunks respecting max_chars
    chunks: list[Chunk] = []
    current_parts: list[str] = []
    current_len = 0
    overlap_chars = int(max_chars * overlap_ratio)

    for sent in sentences:
        if current_len + len(sent) > max_chars and current_parts:
            # Emit chunk
            chunk_text_raw = " ".join(current_parts)
            chunk_text_restored = _restore_code_fences(chunk_text_raw, fences)
            chunks.append(Chunk(
                text=chunk_text_restored,
                source_url=source_url,
                title=title,
                position=len(chunks),
            ))

            # Overlap: keep tail sentences that fit within overlap budget
            overlap_parts: list[str] = []
            overlap_len = 0
            for part in reversed(current_parts):
                if overlap_len + len(part) > overlap_chars:
                    break
                overlap_parts.insert(0, part)
                overlap_len += len(part)

            current_parts = overlap_parts
            current_len = overlap_len

        current_parts.append(sent)
        current_len += len(sent)

    # Emit final chunk
    if current_parts:
        chunk_text_raw = " ".join(current_parts)
        chunk_text_restored = _restore_code_fences(chunk_text_raw, fences)

        # Merge with last chunk if too small
        if len(chunk_text_restored) < min_chars and chunks:
            chunks[-1].text += "\n\n" + chunk_text_restored
        else:
            chunks.append(Chunk(
                text=chunk_text_restored,
                source_url=source_url,
                title=title,
                position=len(chunks),
            ))

    return chunks
