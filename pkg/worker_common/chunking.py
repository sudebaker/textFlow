"""
Shared text chunking utilities for async workers.

Provides character-based chunking for workers that don't need token-based
chunking (audio, image). Extraction-worker uses its own token-based chunking
with tiktoken and overlap support.
"""

from typing import Any, Dict, List

CHARS_PER_TOKEN = 4


def chunk_text(text: str, max_chars: int = 1500) -> List[Dict[str, Any]]:
    """Split text into character-based chunks of roughly max_chars.

    Each chunk has the same structure as extraction-worker's token-based chunks,
    so downstream workers receive identical chunk dictionaries regardless of
    source (audio, image, or extraction fallback).

    Args:
        text: Full document text to chunk.
        max_chars: Maximum characters per chunk (default 1500).

    Returns:
        List of chunk dictionaries, each with:
            chunk_id (str): Unique identifier (chunk_000, chunk_001, ...)
            text (str): Chunk text content.
            start_offset (int): Starting character index in original text.
            end_offset (int): Ending character index (exclusive).
            token_count (int): Estimated token count ((end - start) // 4).
    """
    chunks: List[Dict[str, Any]] = []
    start = 0
    chunk_num = 0

    while start < len(text):
        end = min(start + max_chars, len(text))
        chunks.append({
            "chunk_id": f"chunk_{chunk_num:03d}",
            "text": text[start:end],
            "start_offset": start,
            "end_offset": end,
            "token_count": (end - start) // CHARS_PER_TOKEN,
        })
        if end >= len(text):
            break
        start = end
        chunk_num += 1

    return chunks
