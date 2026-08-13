"""
Image ingestion — caption-and-index for KB.

From chats L293, L298, L302:
  "Caption-and-index: cheaper, reuses existing chunk/embed pipeline.
   While uploading or chunking time we enter overview of the image
   so we can chunk easily. If added in context window directly by
   +button then no embedding required — model supports multimodal
   input, it can just process it directly."

Two modes:
  1. KB mode: image → caption (via LLM vision) → text chunk → embed → store
  2. Context mode: image → forward directly to multimodal model (no embed)

For KB mode, we use NVIDIA NIM vision model to generate a text
description/caption, then feed that into the standard text pipeline.
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass
from typing import Optional

from ..llm.client import NIMClient, get_client

logger = logging.getLogger(__name__)

_SUPPORTED_IMAGES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff"}

_CAPTION_PROMPT = (
    "Describe this image in detail for a knowledge base. Include:\n"
    "1. What the image shows (objects, people, scenes, diagrams)\n"
    "2. Any text visible in the image\n"
    "3. The context/purpose (is it a chart, screenshot, photo, diagram?)\n"
    "4. Key data points if it's a chart/graph/table\n"
    "5. Layout and structure if it's a UI/diagram\n\n"
    "Be specific and factual. This description will be used for "
    "search and retrieval, so include searchable details."
)


@dataclass
class ImageResult:
    """Result of image processing."""
    caption: str = ""           # text description for KB indexing
    base64_data: str = ""       # base64-encoded image for context window
    mime_type: str = ""         # image/png, image/jpeg, etc.
    filename: str = ""
    mode: str = ""              # "kb" or "context"


def is_image_file(filename: str) -> bool:
    """Check if a file is a supported image format."""
    _, ext = os.path.splitext(filename.lower())
    return ext in _SUPPORTED_IMAGES


def _get_mime_type(filename: str) -> str:
    """Get MIME type from filename."""
    ext = os.path.splitext(filename.lower())[1]
    mime_map = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
        ".tiff": "image/tiff",
    }
    return mime_map.get(ext, "image/png")


async def ingest_image_for_kb(
    image_path: str = "",
    image_bytes: bytes = b"",
    filename: str = "",
    *,
    client: Optional[NIMClient] = None,
) -> ImageResult:
    """Process image for Knowledge Base storage.

    Generates a text caption that gets chunked/embedded like normal text.
    The caption IS the searchable representation of this image.
    """
    client = client or get_client()

    # Read image
    if not image_bytes and image_path:
        try:
            with open(image_path, "rb") as f:
                image_bytes = f.read()
            filename = filename or os.path.basename(image_path)
        except Exception as e:
            return ImageResult(caption=f"Error reading image: {e}", mode="kb")

    if not image_bytes:
        return ImageResult(caption="No image data provided", mode="kb")

    mime = _get_mime_type(filename or "image.png")
    b64 = base64.b64encode(image_bytes).decode("utf-8")

    # ── Generate caption via vision model ──
    try:
        # NVIDIA NIM vision-capable model
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _CAPTION_PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{b64}"},
                    },
                ],
            }
        ]

        caption = await client.chat(messages, temperature=0.1, max_tokens=500)
    except Exception as e:
        logger.warning("Vision model failed, using filename as caption: %s", e)
        caption = f"Image: {filename or 'unnamed'}. [Vision captioning unavailable]"

    # Add metadata to caption for better retrieval
    full_caption = f"[Image: {filename}]\n{caption}"

    return ImageResult(
        caption=full_caption,
        base64_data=b64,
        mime_type=mime,
        filename=filename,
        mode="kb",
    )


def prepare_image_for_context(
    image_path: str = "",
    image_bytes: bytes = b"",
    filename: str = "",
) -> ImageResult:
    """Prepare image for direct context window injection (+button).

    No captioning needed — the multimodal model handles it directly.
    Just encode and return.
    """
    if not image_bytes and image_path:
        try:
            with open(image_path, "rb") as f:
                image_bytes = f.read()
            filename = filename or os.path.basename(image_path)
        except Exception as e:
            return ImageResult(caption=f"Error: {e}", mode="context")

    if not image_bytes:
        return ImageResult(caption="No image data", mode="context")

    mime = _get_mime_type(filename or "image.png")
    b64 = base64.b64encode(image_bytes).decode("utf-8")

    return ImageResult(
        base64_data=b64,
        mime_type=mime,
        filename=filename,
        mode="context",
    )
