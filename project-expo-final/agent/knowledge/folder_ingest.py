"""
Folder/file ingestion — walk directory tree → detect → chunk → embed → store.

Supports:
  Text/Code: .py, .js, .ts, .md, .txt, .json, .yaml, .go, .rs, .java, .html, .css
  Documents: .pdf, .docx, .pptx, .xlsx (via dedicated ingestors)
  Images:    .png, .jpg, .jpeg, .webp, .gif (via caption-and-index)

For code files: function-level splitting via regex (lightweight AST)
For text files: sentence-boundary semantic chunking
For docs/images: dedicated ingestors → Markdown → standard chunking
"""

from __future__ import annotations

import logging
import os
import re
from typing import Optional

from ..blocks.semantic.chunk import chunk_text
from ..blocks.semantic.embed import embed_chunks
from ..blocks.semantic.types import Chunk
from ..llm.client import NIMClient, get_client
from .kb_store import KBStore, get_kb_store

logger = logging.getLogger(__name__)

_SUPPORTED_EXTENSIONS = {
    ".py", ".js", ".ts", ".md", ".txt", ".json", ".yaml", ".yml",
    ".go", ".rs", ".java", ".jsx", ".tsx", ".css", ".html",
}

_DOC_EXTENSIONS = {".pdf", ".docx", ".pptx", ".xlsx"}
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}

_ALL_SUPPORTED = _SUPPORTED_EXTENSIONS | _DOC_EXTENSIONS | _IMAGE_EXTENSIONS

_CODE_EXTENSIONS = {".py", ".js", ".ts", ".go", ".rs", ".java", ".jsx", ".tsx"}

# Lightweight function boundary detection (not full AST, but fast)
_FUNC_PATTERNS = {
    ".py": re.compile(
        r"^((?:async\s+)?def\s+\w+|class\s+\w+)", re.MULTILINE
    ),
    ".js": re.compile(
        r"^(?:(?:export\s+)?(?:async\s+)?function\s+\w+|(?:const|let|var)\s+\w+\s*=\s*(?:async\s+)?\(|class\s+\w+)",
        re.MULTILINE,
    ),
    ".ts": re.compile(
        r"^(?:(?:export\s+)?(?:async\s+)?function\s+\w+|(?:const|let|var)\s+\w+\s*[:=]|class\s+\w+|interface\s+\w+)",
        re.MULTILINE,
    ),
    ".go": re.compile(r"^(?:func\s+|type\s+\w+\s+struct)", re.MULTILINE),
    ".rs": re.compile(r"^(?:(?:pub\s+)?fn\s+|(?:pub\s+)?struct\s+|impl\s+)", re.MULTILINE),
    ".java": re.compile(
        r"^(?:\s*(?:public|private|protected)\s+(?:static\s+)?(?:class|interface|(?:\w+\s+)?(?:void|int|String))\s+\w+)",
        re.MULTILINE,
    ),
}


def _chunk_code_file(text: str, source_url: str, ext: str) -> list[Chunk]:
    """Split code file by function/class boundaries."""
    pattern = _FUNC_PATTERNS.get(ext)
    if not pattern:
        return chunk_text(text, source_url)

    matches = list(pattern.finditer(text))
    if not matches:
        return chunk_text(text, source_url)

    chunks = []
    for i, match in enumerate(matches):
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[start:end].strip()

        if len(block) < 30:
            continue

        chunks.append(Chunk(
            text=block,
            source_url=source_url,
            title=match.group(0).strip()[:100],
            position=i,
        ))

    # If header text exists before the first function
    if matches and matches[0].start() > 50:
        header = text[:matches[0].start()].strip()
        if header:
            chunks.insert(0, Chunk(
                text=header,
                source_url=source_url,
                title="module header",
                position=-1,
            ))

    return chunks if chunks else chunk_text(text, source_url)


def walk_and_chunk(
    root_path: str,
    source_prefix: str = "local",
) -> list[Chunk]:
    """Walk a directory, read supported files, and chunk them."""
    all_chunks: list[Chunk] = []
    pending_docs: list[tuple[str, str, str]] = []   # (fpath, source_url, ext)
    pending_images: list[tuple[str, str]] = []       # (fpath, source_url)

    for dirpath, dirnames, filenames in os.walk(root_path):
        # Skip hidden dirs and common noise
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith(".")
            and d not in ("node_modules", "__pycache__", "venv", ".venv",
                          "dist", "build", ".git", "vendor")
        ]

        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in _ALL_SUPPORTED:
                continue

            fpath = os.path.join(dirpath, fname)
            rel_path = os.path.relpath(fpath, root_path).replace("\\", "/")
            source_url = f"kb://{source_prefix}/{rel_path}"

            # Documents — queue for async processing
            if ext in _DOC_EXTENSIONS:
                pending_docs.append((fpath, source_url, ext))
                continue

            # Images — queue for async captioning
            if ext in _IMAGE_EXTENSIONS:
                pending_images.append((fpath, source_url))
                continue

            # Text/code — process inline
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except Exception as e:
                logger.warning("Cannot read %s: %s", fpath, e)
                continue

            if not text.strip():
                continue

            if len(text) > 100_000:
                text = text[:100_000] + "\n\n… [file truncated at 100K chars]"

            if ext in _CODE_EXTENSIONS:
                chunks = _chunk_code_file(text, source_url, ext)
            else:
                chunks = chunk_text(text, source_url)

            all_chunks.extend(chunks)

    logger.info("Walked %s: %d text/code chunks, %d docs pending, %d images pending",
                root_path, len(all_chunks), len(pending_docs), len(pending_images))
    return all_chunks, pending_docs, pending_images


async def ingest_folder(
    folder_path: str,
    *,
    source_prefix: str = "local",
    client: Optional[NIMClient] = None,
    kb: Optional[KBStore] = None,
) -> int:
    """Full pipeline: walk → chunk → embed → store in KB. Returns chunk count.

    Handles text/code (inline), PDFs, DOCX/PPTX/XLSX, and images.
    """
    client = client or get_client()
    kb = kb or get_kb_store()

    text_chunks, pending_docs, pending_images = walk_and_chunk(folder_path, source_prefix)

    # ── Process documents (PDF/DOCX/PPTX/XLSX) ──
    doc_chunks = []
    for fpath, source_url, ext in pending_docs:
        try:
            if ext == ".pdf":
                from .pdf_ingest import ingest_pdf
                result = await ingest_pdf(pdf_path=fpath)
                if result.markdown:
                    doc_chunks.extend(chunk_text(result.markdown, source_url))
            else:
                from .doc_ingest import ingest_document
                result = await ingest_document(fpath)
                if result.markdown:
                    doc_chunks.extend(chunk_text(result.markdown, source_url))
        except Exception as e:
            logger.warning("Failed to ingest %s: %s", fpath, e)

    # ── Process images (caption-and-index) ──
    img_chunks = []
    for fpath, source_url in pending_images:
        try:
            from .image_ingest import ingest_image_for_kb
            result = await ingest_image_for_kb(image_path=fpath, client=client)
            if result.caption:
                img_chunks.append(Chunk(
                    text=result.caption,
                    source_url=source_url,
                    title=f"Image: {result.filename}",
                    position=0,
                ))
        except Exception as e:
            logger.warning("Failed to caption %s: %s", fpath, e)

    # ── Combine all chunks ──
    all_chunks = text_chunks + doc_chunks + img_chunks
    if not all_chunks:
        return 0

    # Embed in batches
    all_chunks = await embed_chunks(all_chunks, client=client)

    # Store in vector KB
    kb.add_chunks(all_chunks)
    kb.rebuild_matrix()
    kb.save()

    # ── Knowledge graph entity extraction (from doc chunks) ──
    graph_triples = 0
    try:
        from .graph_store import extract_entities, get_graph_store
        gs = get_graph_store()

        # Extract from document chunks (most entity-rich content)
        entity_sources = doc_chunks[:10] if doc_chunks else all_chunks[:5]
        for chunk in entity_sources:
            triples = await extract_entities(chunk.text, chunk.source_url, client=client)
            if triples:
                gs.add_triples(triples)
                graph_triples += len(triples)

        if graph_triples:
            gs.save()
    except Exception as e:
        logger.debug("Graph extraction skipped: %s", e)

    logger.info(
        "Ingested folder %s: %d text/code + %d doc + %d image = %d total chunks, "
        "%d graph triples. KB: %d entries",
        folder_path, len(text_chunks), len(doc_chunks), len(img_chunks),
        len(all_chunks), graph_triples, kb.size,
    )
    return len(all_chunks)
