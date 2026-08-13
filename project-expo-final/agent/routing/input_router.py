"""
Input router — decides where uploaded files go: context window vs KB.

From chats L343:
  "Context window: folders go direct to KB, other files can go to model.
   In KB we are not using visual embedding for images and PDF — we are
   using caption-and-index technique."

Routing rules:
  Single text file (< 4000 tokens) → context window (directly to model)
  Single image (via +button)       → context window (base64 multimodal)
  Folder                           → KB (chunk → embed → store)
  Large file (> 4000 tokens)       → KB (chunk → embed → store)
  PDF (any size)                   → KB (PyMuPDF → MD → chunk → embed)
  Multiple files                   → KB
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Optional, Literal

logger = logging.getLogger(__name__)

CONTEXT_TOKEN_LIMIT = 4000  # ~16K chars
CHARS_PER_TOKEN = 4


@dataclass
class RoutedFile:
    """One file with its routing decision."""
    path: str
    filename: str
    route: Literal["context", "kb"]
    reason: str
    content: str = ""           # for context-routed files
    base64_data: str = ""       # for context-routed images
    mime_type: str = ""


@dataclass
class RouteResult:
    """Result of routing a batch of files."""
    context_files: list[RoutedFile] = field(default_factory=list)
    kb_files: list[RoutedFile] = field(default_factory=list)
    kb_folders: list[str] = field(default_factory=list)


_TEXT_EXTENSIONS = {
    ".py", ".js", ".ts", ".md", ".txt", ".json", ".yaml", ".yml",
    ".html", ".css", ".sql", ".sh", ".toml", ".cfg", ".ini",
    ".jsx", ".tsx", ".go", ".rs", ".java", ".kt", ".c", ".cpp", ".h",
    ".xml", ".csv", ".env", ".conf",
}

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
_DOC_EXTENSIONS = {".pdf", ".docx", ".pptx", ".xlsx"}


def route_inputs(
    paths: list[str],
    *,
    mode: Literal["auto", "kb", "context"] = "auto",
) -> RouteResult:
    """Route a batch of file/folder paths to context window or KB.

    mode="auto" uses smart heuristics per the chat rules.
    mode="kb" forces everything to KB.
    mode="context" forces everything to context window (may fail for large files).
    """
    result = RouteResult()

    for path in paths:
        # ── Folders always go to KB ──
        if os.path.isdir(path):
            result.kb_folders.append(path)
            continue

        if not os.path.isfile(path):
            logger.warning("Skipping non-existent path: %s", path)
            continue

        filename = os.path.basename(path)
        ext = os.path.splitext(filename)[1].lower()

        # Force modes
        if mode == "kb":
            result.kb_files.append(RoutedFile(
                path=path, filename=filename, route="kb",
                reason="Force KB mode",
            ))
            continue
        if mode == "context":
            routed = _route_to_context(path, filename, ext)
            result.context_files.append(routed)
            continue

        # ── Auto mode: smart routing ──

        # PDFs always go to KB (need extraction pipeline)
        if ext == ".pdf":
            result.kb_files.append(RoutedFile(
                path=path, filename=filename, route="kb",
                reason="PDF: requires extraction pipeline",
            ))
            continue

        # DOCX/PPTX/XLSX go to KB (need dedicated ingestors)
        if ext in _DOC_EXTENSIONS:
            result.kb_files.append(RoutedFile(
                path=path, filename=filename, route="kb",
                reason=f"{ext}: requires document ingestor",
            ))
            continue

        # Images: context window (base64 to multimodal model)
        if ext in _IMAGE_EXTENSIONS:
            result.context_files.append(RoutedFile(
                path=path, filename=filename, route="context",
                reason="Image: base64 to multimodal model (no embedding)",
            ))
            continue

        # Text files: route by size
        if ext in _TEXT_EXTENSIONS or ext == "":
            try:
                size = os.path.getsize(path)
                token_estimate = size / CHARS_PER_TOKEN

                if token_estimate <= CONTEXT_TOKEN_LIMIT:
                    # Small enough for context window
                    try:
                        with open(path, "r", encoding="utf-8", errors="replace") as f:
                            content = f.read()
                        result.context_files.append(RoutedFile(
                            path=path, filename=filename, route="context",
                            reason=f"Small text ({int(token_estimate)} tokens < {CONTEXT_TOKEN_LIMIT})",
                            content=content,
                        ))
                    except Exception:
                        result.kb_files.append(RoutedFile(
                            path=path, filename=filename, route="kb",
                            reason="Read error, falling back to KB",
                        ))
                else:
                    # Too large for context window
                    result.kb_files.append(RoutedFile(
                        path=path, filename=filename, route="kb",
                        reason=f"Large text ({int(token_estimate)} tokens > {CONTEXT_TOKEN_LIMIT})",
                    ))
            except OSError:
                result.kb_files.append(RoutedFile(
                    path=path, filename=filename, route="kb",
                    reason="Cannot determine size",
                ))
            continue

        # Unknown extension: try KB
        result.kb_files.append(RoutedFile(
            path=path, filename=filename, route="kb",
            reason=f"Unknown extension {ext}",
        ))

    # Log routing decisions
    logger.info(
        "Input routing: %d→context, %d→KB files, %d→KB folders",
        len(result.context_files), len(result.kb_files), len(result.kb_folders),
    )

    return result


def _route_to_context(path: str, filename: str, ext: str) -> RoutedFile:
    """Force-route a file to context window."""
    if ext in _IMAGE_EXTENSIONS:
        return RoutedFile(
            path=path, filename=filename, route="context",
            reason="Image: forced context mode",
        )

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return RoutedFile(
            path=path, filename=filename, route="context",
            reason="Forced context mode",
            content=content,
        )
    except Exception:
        return RoutedFile(
            path=path, filename=filename, route="context",
            reason="Forced context mode (read failed)",
        )
