"""
Progressive loader — GeoHash-style information loading.

From chats L343:
  "Redis geo has — first 4 chars gives you some overview of location,
   after increasing each letter you get zoomed in to specific.
   Like this concept of not loading unnecessary or ambiguous
   not-decidable things, we can just inherit it like geohash
   and then going on specific we can choose paths."

This principle applies to:
  1. Folder upload → first scan structure + README, then drill into files
  2. Retrieval → first get titles+snippets, only fetch full pages for top-ranked
  3. File generation → first plan structure, then implement file by file
  4. Knowledge base → overview first, detail on demand

The key insight: don't load EVERYTHING at once. Load the OVERVIEW first,
then progressively drill into the parts that matter for this specific query.
"""

from __future__ import annotations

import os
import logging
from dataclasses import dataclass, field
from typing import Optional, Callable, Awaitable

logger = logging.getLogger(__name__)


@dataclass
class ProgressiveLevel:
    """One level of progressive detail."""
    depth: int                       # 0 = overview, 1 = sections, 2 = full
    content: str                     # the content at this level
    path: str = ""                   # file/section path
    is_relevant: bool = True         # whether this is relevant to the query
    children_count: int = 0          # how many children could be expanded


@dataclass
class ProgressiveResult:
    """Result of progressive loading at a specific depth."""
    levels: list[ProgressiveLevel] = field(default_factory=list)
    total_available: int = 0         # total items that could be loaded
    loaded_count: int = 0            # items actually loaded at full depth
    depth_reached: int = 0


# ── Folder progressive loading ──

async def progressive_folder_scan(
    folder_path: str,
    query: str = "",
    max_depth: int = 2,
) -> ProgressiveResult:
    """Scan a folder progressively:
    Depth 0: file tree + sizes only
    Depth 1: file tree + first 50 lines of each file + README
    Depth 2: full content of relevant files only
    """
    result = ProgressiveResult()

    if not os.path.isdir(folder_path):
        return result

    # ── Depth 0: Structure only ──
    tree_lines = []
    file_count = 0

    for root, dirs, files in os.walk(folder_path):
        # Skip hidden/node_modules/venv
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in
                   ("node_modules", "__pycache__", "venv", ".git", "dist", "build")]

        rel_root = os.path.relpath(root, folder_path)
        depth = rel_root.count(os.sep)

        if depth > 3:  # Don't scan too deep in overview
            continue

        indent = "  " * depth
        if rel_root != ".":
            tree_lines.append(f"{indent}📁 {os.path.basename(root)}/")

        for f in sorted(files):
            fp = os.path.join(root, f)
            try:
                size = os.path.getsize(fp)
                size_str = _human_size(size)
            except OSError:
                size_str = "?"
            tree_lines.append(f"{indent}  📄 {f} ({size_str})")
            file_count += 1

    result.levels.append(ProgressiveLevel(
        depth=0,
        content="\n".join(tree_lines),
        path=folder_path,
        children_count=file_count,
    ))
    result.total_available = file_count

    if max_depth < 1:
        return result

    # ── Depth 1: README + file previews ──
    previews = []
    readme_content = ""

    for root, dirs, files in os.walk(folder_path):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in
                   ("node_modules", "__pycache__", "venv", ".git")]

        for f in sorted(files):
            fp = os.path.join(root, f)
            rel = os.path.relpath(fp, folder_path)

            # README gets full treatment
            if f.lower() in ("readme.md", "readme.txt", "readme"):
                try:
                    with open(fp, "r", encoding="utf-8", errors="replace") as fh:
                        readme_content = fh.read()[:5000]
                except Exception:
                    pass
                continue

            # Other files: first 10 lines only
            if _is_text_file(f):
                try:
                    with open(fp, "r", encoding="utf-8", errors="replace") as fh:
                        head = "".join(fh.readline() for _ in range(10))
                    previews.append(f"--- {rel} ---\n{head}")
                except Exception:
                    pass

    depth1_content = ""
    if readme_content:
        depth1_content += f"## README\n{readme_content}\n\n"
    depth1_content += "## File Previews\n" + "\n\n".join(previews[:20])

    result.levels.append(ProgressiveLevel(
        depth=1,
        content=depth1_content,
        path=folder_path,
        children_count=len(previews),
    ))

    result.depth_reached = 1
    return result


# ── Retrieval progressive loading ──

def progressive_source_filter(
    sources: list[dict],
    query: str,
    top_k: int = 5,
) -> list[dict]:
    """GeoHash for retrieval: only fetch full content for top-k sources.

    Sources with rich snippets skip fetching entirely (already at depth 1).
    Only sources ranked in top-k AND lacking snippets get full fetch (depth 2).

    This is the "not loading unnecessary" principle applied to web retrieval.
    """
    # Depth 0: all sources (just URLs + titles)
    # Depth 1: sources with snippets (already have content)
    # Depth 2: top-k sources without snippets (need fetching)

    enriched = []
    needs_fetch = []

    for source in sources:
        snippet = source.get("snippet", "") or source.get("extra_snippets", "")
        if snippet and len(snippet) > 100:
            # Already at depth 1 — has content, skip fetch
            source["_progressive_depth"] = 1
            source["_needs_fetch"] = False
            enriched.append(source)
        else:
            # Needs fetch to reach depth 2
            source["_progressive_depth"] = 0
            source["_needs_fetch"] = True
            needs_fetch.append(source)

    # Only fetch the top-k that need it (not ALL unfetched sources)
    for source in needs_fetch[:top_k]:
        source["_needs_fetch"] = True
        enriched.append(source)

    # Rest get dropped (not worth fetching)
    for source in needs_fetch[top_k:]:
        source["_needs_fetch"] = False
        enriched.append(source)

    return enriched


# ── Context window progressive loading ──

def progressive_context_budget(
    items: list[str],
    max_tokens_estimate: int = 4000,
    chars_per_token: int = 4,
) -> list[str]:
    """Fit items into a context budget progressively.

    First pass: include all items truncated to overview (first 200 chars).
    Second pass: expand the most relevant items to full.
    This ensures we always have SOME coverage of everything,
    with FULL coverage of the most important items.
    """
    max_chars = max_tokens_estimate * chars_per_token
    result = []
    used = 0

    # Pass 1: overviews of everything
    overviews = []
    for item in items:
        overview = item[:200] + ("..." if len(item) > 200 else "")
        overviews.append(overview)
        used += len(overview)

    if used <= max_chars:
        # Room for full items — expand in order of length (shorter = more dense info)
        remaining = max_chars - used
        sorted_items = sorted(enumerate(items), key=lambda x: len(x[1]))

        expanded = set()
        for idx, full_text in sorted_items:
            extra_chars = len(full_text) - len(overviews[idx])
            if extra_chars <= remaining:
                overviews[idx] = full_text
                remaining -= extra_chars
                expanded.add(idx)

        return overviews
    else:
        # Even overviews exceed budget — truncate from the end
        truncated = []
        remaining = max_chars
        for ov in overviews:
            if remaining <= 0:
                break
            take = min(len(ov), remaining)
            truncated.append(ov[:take])
            remaining -= take
        return truncated


# ── Helpers ──

def _human_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


_TEXT_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".css", ".scss",
    ".json", ".yaml", ".yml", ".toml", ".md", ".txt", ".rst",
    ".sh", ".bash", ".zsh", ".fish", ".ps1",
    ".java", ".kt", ".go", ".rs", ".c", ".cpp", ".h",
    ".sql", ".graphql", ".proto", ".xml", ".csv",
    ".env", ".gitignore", ".dockerfile", ".conf", ".cfg", ".ini",
}


def _is_text_file(filename: str) -> bool:
    _, ext = os.path.splitext(filename.lower())
    return ext in _TEXT_EXTENSIONS or not ext  # extensionless = probably text
