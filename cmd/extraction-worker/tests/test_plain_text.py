"""Plain-text extraction fast path.

`.txt` is an admitted extension and batch ingestion encodes documents as
`.txt`, but Docling returns empty text for plain text. These tests pin that
the extraction worker bypasses Docling for `.txt` payloads.
"""

import asyncio
import base64
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

for _mod in (
    "aio_pika",
    "aio_pika.abc",
    "aiohttp",
    "langdetect",
    "magic",
    "redis",
    "textstat",
    "tiktoken",
    "prometheus_client",
):
    sys.modules.setdefault(_mod, MagicMock())

import worker


def _worker_without_docling() -> worker.ExtractionWorker:
    instance = object.__new__(worker.ExtractionWorker)

    async def _boom(*args, **kwargs):
        raise AssertionError("Docling must not be called for plain text")

    instance._docling_convert_async = _boom
    return instance


def test_base64_plain_text_skips_docling():
    instance = _worker_without_docling()
    payload = base64.b64encode("Hola mundo\n".encode("utf-8")).decode()

    result = asyncio.run(
        instance.extract_text_from_base64(payload, filename="note.txt")
    )

    assert result["text"] == "Hola mundo\n"
    assert result["metadata"]["extraction_method"] == "plain_text"


def test_file_plain_text_skips_docling(tmp_path):
    instance = _worker_without_docling()
    path = tmp_path / "note.txt"
    path.write_text("Linea uno\nLinea dos\n", encoding="utf-8")

    result = asyncio.run(instance.extract_text_from_file(str(path), "note.txt"))

    assert result["text"] == "Linea uno\nLinea dos\n"
    assert result["metadata"]["extraction_method"] == "plain_text"
