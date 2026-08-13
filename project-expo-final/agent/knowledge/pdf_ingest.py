"""
PDF ingestion — extract text from PDFs for the knowledge base.

From chats L306-307:
  "Native PDFs and scanned PDFs are not the same problem.
   Detection has to happen per page, not per file: try native
   extraction first, fall back to OCR on pages where it comes
   back empty. PyMuPDF with pymupdf4llm converts to clean
   structured Markdown preserving chapters, headings, and lists."

Pipeline: PDF → per-page detect → native text or OCR → Markdown → chunks
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class PDFPage:
    """One extracted page from a PDF."""
    page_num: int
    text: str
    method: str = "native"  # "native" or "ocr"
    has_tables: bool = False
    has_images: bool = False


@dataclass
class PDFResult:
    """Result of PDF ingestion."""
    pages: list[PDFPage] = field(default_factory=list)
    markdown: str = ""
    title: str = ""
    total_pages: int = 0
    native_pages: int = 0
    ocr_pages: int = 0


async def ingest_pdf(
    pdf_path: str = "",
    pdf_bytes: bytes = b"",
) -> PDFResult:
    """Extract text from a PDF, producing Markdown output.

    Tries PyMuPDF (fitz) first for native extraction.
    Falls back to per-page OCR detection if native returns empty.

    Args:
        pdf_path: path to PDF file
        pdf_bytes: raw PDF bytes (alternative to path)
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        logger.error("PyMuPDF (fitz) not installed. Install with: pip install pymupdf")
        return PDFResult(markdown="Error: PyMuPDF not installed")

    # Open PDF
    try:
        if pdf_bytes:
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        elif pdf_path:
            doc = fitz.open(pdf_path)
        else:
            return PDFResult(markdown="Error: no PDF source provided")
    except Exception as e:
        return PDFResult(markdown=f"Error opening PDF: {e}")

    result = PDFResult(
        total_pages=len(doc),
        title=doc.metadata.get("title", "") if doc.metadata else "",
    )

    md_parts = []
    if result.title:
        md_parts.append(f"# {result.title}\n")

    for page_num in range(len(doc)):
        page = doc[page_num]

        # ── Try native text extraction first ──
        text = page.get_text("text").strip()

        if len(text) > 50:
            # Native extraction worked
            method = "native"
            result.native_pages += 1
        else:
            # Page is likely scanned — try text from blocks
            blocks = page.get_text("blocks")
            text_from_blocks = " ".join(
                b[4] for b in blocks if b[6] == 0  # type 0 = text
            ).strip()

            if len(text_from_blocks) > 50:
                text = text_from_blocks
                method = "native"
                result.native_pages += 1
            else:
                # Truly empty — OCR would be needed
                # For now, mark as OCR-needed and extract what we can
                method = "ocr_needed"
                result.ocr_pages += 1
                text = f"[Page {page_num + 1}: scanned/image-based, OCR required]"

        # ── Check for tables ──
        has_tables = bool(page.find_tables()) if hasattr(page, "find_tables") else False

        # ── Check for images ──
        has_images = len(page.get_images()) > 0

        # ── Extract tables if present ──
        table_md = ""
        if has_tables:
            try:
                tables = page.find_tables()
                for table in tables:
                    rows = table.extract()
                    if rows:
                        # Convert to markdown table
                        header = rows[0]
                        table_md += "\n| " + " | ".join(str(c or "") for c in header) + " |\n"
                        table_md += "| " + " | ".join("---" for _ in header) + " |\n"
                        for row in rows[1:]:
                            table_md += "| " + " | ".join(str(c or "") for c in row) + " |\n"
                        table_md += "\n"
            except Exception:
                pass

        pdf_page = PDFPage(
            page_num=page_num + 1,
            text=text,
            method=method,
            has_tables=has_tables,
            has_images=has_images,
        )
        result.pages.append(pdf_page)

        # ── Build Markdown for this page ──
        if method != "ocr_needed" or not text.startswith("[Page"):
            md_parts.append(f"\n## Page {page_num + 1}\n")
            md_parts.append(_text_to_markdown(text))
            if table_md:
                md_parts.append(table_md)
        else:
            md_parts.append(f"\n## Page {page_num + 1}\n")
            md_parts.append(text)

    doc.close()
    result.markdown = "\n".join(md_parts)
    return result


def _text_to_markdown(text: str) -> str:
    """Convert raw PDF text to cleaner Markdown.

    Handles common PDF text issues:
    - Excessive whitespace
    - Broken paragraphs (lines that should be joined)
    - Bullet points
    """
    lines = text.split("\n")
    md_lines = []
    prev_empty = False

    for line in lines:
        stripped = line.strip()

        if not stripped:
            if not prev_empty:
                md_lines.append("")
                prev_empty = True
            continue
        prev_empty = False

        # Detect bullet points
        if stripped.startswith(("•", "●", "○", "■", "▪")):
            md_lines.append(f"- {stripped[1:].strip()}")
        elif stripped.startswith(("- ", "* ")):
            md_lines.append(stripped)
        else:
            # Join with previous line if it looks like a broken paragraph
            if (md_lines and md_lines[-1] and
                not md_lines[-1].endswith((".", ":", "!", "?", "")) and
                not stripped[0].isupper() and
                len(stripped) > 20):
                md_lines[-1] += " " + stripped
            else:
                md_lines.append(stripped)

    return "\n".join(md_lines)
