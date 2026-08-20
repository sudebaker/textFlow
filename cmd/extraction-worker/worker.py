#!/usr/bin/env python3
"""Document text extraction and metadata pipeline for the orchestrator.

This module implements the first step in the document processing pipeline:
1. Download documents from file paths, base64, or URLs
2. Extract text using Docling API (async polling with configurable timeouts)
3. Extract document metadata (author, creation date, page count, MIME type, etc.)
4. Chunk text into overlapping token-based segments
5. Classify document source type (notariado, catastro, bancario, etc.)
6. Store results to Redis for downstream workers
7. Publish extraction completion to embeddings, entities, and metadata queues

The worker is designed for air-gapped deployments with offline models and no
internet access at runtime. It uses RabbitMQ for async job processing with
automatic retries and graceful error handling.

This worker uses asyncio + aio_pika + aiohttp to enable concurrent processing
of multiple Docling jobs simultaneously (up to EXTRACTION_CONCURRENCY at once),
eliminating the serial bottleneck of blocking I/O.

Environment variables:
    REDIS_URL: Redis connection URL (default: redis://localhost:6379)
    RABBITMQ_URL: RabbitMQ connection URL (default: amqp://localhost:5672/)
    DOCLING_URL: Docling API endpoint (default: http://docling:5001)
    QUEUE_NAME: Input queue for extraction jobs (default: extract_text)
    EXTRACTION_CONCURRENCY: Max concurrent Docling jobs (default: 5)
    PREFETCH_COUNT: Backwards-compat alias for EXTRACTION_CONCURRENCY (default: 5)
    METRICS_PORT: Prometheus metrics port (default: 8004)
    CHUNK_SIZE_TOKENS: Tokens per chunk (default: 512)
    CHUNK_OVERLAP_TOKENS: Overlap between chunks (default: 50)
    EXIFTOOL_PATH: Path to exiftool binary (default: /usr/bin/exiftool)
    DOCLING_DO_OCR: Enable OCR for text-based PDFs (default: false)
    DOCLING_OCR_ENGINE: OCR engine to use (default: rapidocr)
    DOCLING_CONVERSION_TIMEOUT: Max seconds for Docling conversion (default: 1800)
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import aio_pika
import aio_pika.abc
import aiohttp
import langdetect
import magic
import redis
import textstat
import tiktoken
from prometheus_client import Counter, Histogram, start_http_server

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
from pkg.events_python import EventBus
from pkg.worker_common.artifact_store import STORE
from pkg.worker_common.rabbitmq_async import declare_queue_async

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Prometheus metrics
jobs_total = Counter("extraction_worker_jobs_total", "Total jobs processed", ["status"])
job_duration = Histogram(
    "extraction_worker_job_duration_seconds", "Job processing duration in seconds"
)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://localhost:5672/")
DOCLING_URL = os.getenv("DOCLING_URL", "http://docling:5001")
QUEUE_NAME = os.getenv("QUEUE_NAME", "extract_text")
# EXTRACTION_CONCURRENCY replaces PREFETCH_COUNT for the extraction worker.
# Falls back to PREFETCH_COUNT for backwards compatibility with docker-compose.yml.
PREFETCH_COUNT = int(os.getenv("EXTRACTION_CONCURRENCY", os.getenv("PREFETCH_COUNT", "5")))
METRICS_PORT = int(os.getenv("METRICS_PORT", "8004"))

CHUNK_SIZE_TOKENS = int(os.getenv("CHUNK_SIZE_TOKENS", "512"))
CHUNK_OVERLAP_TOKENS = int(os.getenv("CHUNK_OVERLAP_TOKENS", "50"))
EXIFTOOL_PATH = os.getenv("EXIFTOOL_PATH", "/usr/bin/exiftool")

# Docling OCR settings — disable by default (text PDFs don't need OCR).
# Set DOCLING_DO_OCR=true to re-enable; change DOCLING_OCR_ENGINE to easyocr
# only if EasyOCR model files are present under /models/docling/EasyOcr/.
DOCLING_DO_OCR = os.getenv("DOCLING_DO_OCR", "false").lower() == "true"
DOCLING_OCR_ENGINE = os.getenv("DOCLING_OCR_ENGINE", "rapidocr")
# Maximum seconds to wait for a single docling conversion (async polling).
DOCLING_CONVERSION_TIMEOUT = int(os.getenv("DOCLING_CONVERSION_TIMEOUT", "1800"))

try:
    tokenizer = tiktoken.get_encoding("cl100k_base")
except Exception:
    logger.warning("tiktoken online failed, using simple tokenization")
    tokenizer = None


def compute_file_hash(file_bytes: bytes) -> str:
    """Compute SHA-256 hash digest of raw file bytes.

    Used for document deduplication and integrity verification. Provides a
    consistent hash regardless of file origin (path, base64, URL).

    Args:
        file_bytes: Raw binary content of document file.

    Returns:
        Hexadecimal SHA-256 hash digest of file contents.

    Example:
        >>> hash_val = compute_file_hash(b"Hello World")
        >>> print(len(hash_val))
        64  # SHA-256 produces 64 hex characters
    """
    return hashlib.sha256(file_bytes).hexdigest()


def extract_pdf_metadata(file_path: str, filename: str) -> Dict[str, Any]:
    """Extract document-level metadata using exiftool.

    Parses EXIF and XMP metadata from PDF, DOCX, images, and other document
    types using the exiftool binary. Gracefully handles missing exiftool or
    corrupted files by returning partially filled metadata.

    Falls back to filesystem attributes (size, MIME type) if metadata extraction
    fails. All fields are populated with None/empty values on extraction failure
    rather than raising exceptions, ensuring downstream processing can continue.

    Args:
        file_path: Absolute path to document file on disk.
        filename: Filename for metadata record (may differ from file_path basename).

    Returns:
        Dictionary with extracted metadata:
            filename (str): Document filename.
            file_size_bytes (int): File size in bytes (0 if file not found).
            sha256 (str): SHA-256 hash of file contents (empty if read fails).
            author (str|None): Document author (from Author or Creator EXIF tags).
            title (str|None): Document title (from Title or DocumentTitle tags).
            subject (str|None): Document subject from EXIF.
            creator (str|None): Document creator software/tool.
            producer (str|None): PDF producer software.
            creation_date (str|None): Creation timestamp (ISO format if available).
            modification_date (str|None): Last modification timestamp.
            page_count (int|None): Number of pages (PDF/DOCX).
            encrypted (bool): Whether document is password-protected.
            mime_type (str|None): MIME type (detected via libmagic).
            exif_data (dict): Full EXIF tag dictionary (excluding system tags).

    Raises:
        No exceptions raised. All failures result in None/empty values in metadata.

    Note:
        exiftool is called with a 30-second timeout to avoid hanging on large
        or corrupt files. Requires exiftool binary at EXIFTOOL_PATH.
    """
    metadata = {
        "filename": filename,
        "file_size_bytes": os.path.getsize(file_path) if os.path.exists(file_path) else 0,
        "sha256": "",
        "author": None,
        "title": None,
        "subject": None,
        "creator": None,
        "producer": None,
        "creation_date": None,
        "modification_date": None,
        "page_count": None,
        "encrypted": False,
        "mime_type": None,
        "exif_data": {},
    }

    if os.path.exists(file_path):
        with open(file_path, "rb") as f:
            file_bytes = f.read()
            metadata["sha256"] = compute_file_hash(file_bytes)

        metadata["mime_type"] = magic.from_file(file_path, mime=True)

        try:
            result = subprocess.run(
                [EXIFTOOL_PATH, "-j", file_path],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                exif_data = json.loads(result.stdout)
                if exif_data:
                    exif = exif_data[0]

                    metadata["author"] = exif.get("Author") or exif.get("Creator")
                    metadata["title"] = exif.get("Title") or exif.get("DocumentTitle")
                    metadata["subject"] = exif.get("Subject")
                    metadata["creator"] = exif.get("Creator") or exif.get("Software")
                    metadata["producer"] = exif.get("Producer")

                    if "CreateDate" in exif:
                        metadata["creation_date"] = exif["CreateDate"]
                    elif "CreationDate" in exif:
                        metadata["creation_date"] = exif["CreationDate"]

                    if "ModifyDate" in exif:
                        metadata["modification_date"] = exif["ModifyDate"]

                    if "PageCount" in exif:
                        metadata["page_count"] = int(exif["PageCount"])

                    metadata["encrypted"] = exif.get("Encrypted", False)

                    metadata["exif_data"] = {
                        k: v
                        for k, v in exif.items()
                        if k not in ["SourceFile", "File:FileSize", "File:MIMEType"]
                    }
        except Exception as e:
            logger.warning(f"exiftool extraction failed: {e}")

    return metadata


def chunk_text(
    text: str, chunk_size: int = CHUNK_SIZE_TOKENS, overlap: int = CHUNK_OVERLAP_TOKENS
) -> List[Dict[str, Any]]:
    """Split text into overlapping token-based chunks.

    Creates semantic chunks of configurable size with overlap to prevent losing
    context at chunk boundaries. Uses tiktoken cl100k_base encoding when available
    (online), falling back to character approximation for offline deployments.

    The overlap ensures that important information near chunk boundaries is
    included in multiple chunks, preventing loss of context during downstream
    embedding and entity extraction.

    Args:
        text: Full document text to chunk.
        chunk_size: Target chunk size in tokens (default: CHUNK_SIZE_TOKENS).
                   Actual token counts may vary due to tokenizer boundaries.
        overlap: Overlap between consecutive chunks in tokens (default: CHUNK_OVERLAP_TOKENS).
                Smaller overlaps reduce redundancy; larger overlaps improve context preservation.

    Returns:
        List of chunk dictionaries, each with:
            chunk_id (str): Unique identifier (chunk_000, chunk_001, etc.).
            text (str): Decoded chunk text.
            start_offset (int): Starting token index in original token sequence.
            end_offset (int): Ending token index (exclusive).
            token_count (int): Actual token count of this chunk.

    Note:
        If tiktoken is unavailable (offline mode), falls back to character-based
        approximation using 4 chars per token. Chunk token_count will be estimated
        as (end_offset - start_offset) // 4.

    Example:
        >>> text = "Hello world. This is a test."
        >>> chunks = chunk_text(text, chunk_size=5, overlap=1)
        >>> len(chunks) >= 1
        True
        >>> chunks[0]['chunk_id']
        'chunk_000'
    """
    chunks = []

    if tokenizer is not None:
        tokens = tokenizer.encode(text)
        start = 0
        chunk_num = 0

        while start < len(tokens):
            end = min(start + chunk_size, len(tokens))
            chunk_tokens = tokens[start:end]
            chunk_text_str = tokenizer.decode(chunk_tokens)

            chunks.append(
                {
                    "chunk_id": f"chunk_{chunk_num:03d}",
                    "text": chunk_text_str,
                    "start_offset": start,
                    "end_offset": end,
                    "token_count": len(chunk_tokens),
                }
            )

            if end >= len(tokens):
                break

            start = end - overlap
            chunk_num += 1
    else:
        chars = list(text)
        char_count = len(chars)
        chars_per_token = 4
        effective_chunk_size = chunk_size * chars_per_token
        effective_overlap = overlap * chars_per_token

        start = 0
        chunk_num = 0

        while start < char_count:
            end = min(start + effective_chunk_size, char_count)
            chunk_chars = chars[start:end]
            chunk_text_str = "".join(chunk_chars)

            chunks.append(
                {
                    "chunk_id": f"chunk_{chunk_num:03d}",
                    "text": chunk_text_str,
                    "start_offset": start,
                    "end_offset": end,
                    "token_count": (end - start) // chars_per_token,
                }
            )

            if end >= char_count:
                break

            start = end - effective_overlap
            chunk_num += 1

    logger.info(f"Created {len(chunks)} chunks")
    return chunks


def analyze_text(text: str) -> Dict[str, Any]:
    """Compute lightweight text analytics without ML models.

    Performs fast, offline text analysis suitable for feature engineering and
    downstream pipeline filtering. No neural models required; uses regex patterns
    and heuristics for language detection and content analysis.

    Args:
        text: Full document text to analyze.

    Returns:
        Dictionary with analytics:
            char_count (int): Total characters (including whitespace).
            word_count (int): Words separated by whitespace.
            line_count (int): Lines separated by newline characters.
            language (str): ISO 639-1 language code (e.g., 'es', 'en', 'unknown').
            has_urls (bool): True if text contains http:// or https://.
            has_emails (bool): True if text contains email-like patterns (text@domain).
            has_numbers (bool): True if text contains numeric digits.
            readability_score (float): Flesch Reading Ease score (0-100).
            encoding (str): Character encoding (always 'utf-8').

    Note:
        Language detection uses langdetect library (probability-based). On short
        text or mixed languages, may return 'unknown' instead of best guess.

        Readability calculation uses textstat.flesch_reading_ease():
        - 90-100: Very Easy (5th grade)
        - 60-70: Standard (8th-9th grade)
        - 0-30: Difficult (college level)

        Failures in language detection or readability calculation do not raise
        exceptions; fields are left as 'unknown' or omitted.

    Example:
        >>> analysis = analyze_text("Hello world. This is a test email@example.com")
        >>> analysis['has_emails']
        True
        >>> analysis['language']
        'en'
    """
    analysis = {
        "char_count": len(text),
        "word_count": len(text.split()),
        "line_count": len(text.split("\n")),
        "language": "unknown",
        "has_urls": False,
        "has_emails": False,
        "has_numbers": False,
        "encoding": "utf-8",
    }

    try:
        analysis["language"] = langdetect.detect(text)
    except Exception as e:
        logger.warning(f"Language detection failed: {e}")
        analysis["language"] = "unknown"

    analysis["has_urls"] = "http://" in text or "https://" in text
    analysis["has_emails"] = "@" in text and "." in text.split("@")[-1]

    import re

    numbers = re.findall(r"\d+", text)
    analysis["has_numbers"] = len(numbers) > 0

    try:
        analysis["readability_score"] = textstat.flesch_reading_ease(text)
    except Exception as e:
        logger.warning(f"Readability calculation failed: {e}")

    return analysis


class SourceClassifier:
    """Classify document source type using regex pattern matching.

    Identifies document category (notariado, catastro, bancario, fiscal, legal)
    based on keyword and phrase patterns. Provides confidence scores based on
    pattern matches.

    Supported document types:
        - notariado: Notary documents (escritura, protocolo, etc.)
        - catastro: Cadastral/property records (referencia catastral, etc.)
        - bancario: Bank statements and financial documents.
        - fiscal: Tax/revenue documents (impuesto, declaración fiscal, etc.)
        - legal: Contracts and legal documents.

    Attributes:
        PATTERNS (dict): Mapping of document_type to list of regex patterns.
    """

    # Regex patterns for different document types
    PATTERNS = {
        "notariado": [
            r"notario|notaría|protocolo|escritura|fedatario",
            r"fe pública|acta notarial",
        ],
        "catastro": [
            r"catastro|catastral|referencia catastral",
            r"plano catastral|datos catastrales",
        ],
        "bancario": [
            r"banco|bancaria|entidad financiera",
            r"estado de cuenta|extracto bancario|movimiento",
        ],
        "fiscal": [
            r"impuesto|declaración fiscal|renta",
            r"hacienda|tributario|aeat",
        ],
        "legal": [
            r"contrato|acuerdo|términos y condiciones",
            r"cláusula|párrafo|legal|juzgado",
        ],
    }

    @staticmethod
    def classify(text: str) -> Optional[Dict[str, Any]]:
        """Classify document source type using pattern matching.

        Searches document text for keyword patterns and returns the document
        type with highest confidence. Confidence is calculated as the proportion
        of patterns matched for that type.

        Args:
            text: Full document text to classify.

        Returns:
            Dictionary with classification result:
                document_type (str): Best-matching document type (notariado, catastro, etc.).
                confidence (float): Confidence score 0.0-1.0 (fraction of patterns matched).
                classifier_version (str): Version of classification model ("1.0").

            Returns None if:
                - text is empty or None
                - no patterns match any document type

        Example:
            >>> result = SourceClassifier.classify("Esta es una escritura notarial...")
            >>> result['document_type']
            'notariado'
            >>> result['confidence']
            0.5  # 1 of 2 patterns matched

        Note:
            Comparison is case-insensitive. Partial matches within text are
            sufficient (no word boundary required). This allows flexibility
            with abbreviations and variations.
        """
        if not text:
            return None

        import re

        text_lower = text.lower()
        scores = {}

        for doc_type, patterns in SourceClassifier.PATTERNS.items():
            matches = 0
            for pattern in patterns:
                if re.search(pattern, text_lower, re.IGNORECASE):
                    matches += 1

            if matches > 0:
                # Confidence based on number of matching patterns
                confidence = min(1.0, matches / len(patterns))
                scores[doc_type] = confidence

        if not scores:
            return None

        # Return highest confidence match
        best_type = max(scores.items(), key=lambda x: x[1])
        return {
            "document_type": best_type[0],
            "confidence": float(best_type[1]),
            "classifier_version": "1.0",
        }


class ExtractionWorker:
    """Async RabbitMQ consumer for document extraction and metadata pipeline.

    Processes documents from the extraction queue by:
    1. Downloading documents (file path, base64, or URL)
    2. Extracting text via Docling API with async polling
    3. Extracting document metadata (author, creation date, page count, etc.)
    4. Analyzing text for language, readability, and patterns
    5. Chunking text into overlapping token-based segments
    6. Classifying document source type (notariado, catastro, etc.)
    7. Storing results to Redis (sync — fast, acceptable brief blocking)
    8. Publishing job to downstream queues (embeddings, entities, metadata)

    This is the first step in the document processing pipeline. Results are stored
    in Redis under keys like orchestrator:job:{job_id}:{text,chunks,metadata:document}.

    Concurrent processing is enabled via asyncio.create_task(): up to PREFETCH_COUNT
    Docling jobs can run simultaneously, with each independently polling Docling
    using non-blocking aiohttp calls.

    Attributes:
        redis_client: Sync Redis connection (fast I/O, no need for async).
        event_bus: EventBus instance for job progress updates.
        temp_dir: Temporary directory for storing intermediate files.
    """

    def __init__(self):
        """Initialize extraction worker with Redis client.

        Sets up Redis connection for result storage, initializes event bus for
        job progress updates, and creates temporary directory for intermediate
        files. Resources are cleaned up on deletion.

        Raises:
            redis.ConnectionError: If Redis is unavailable.
        """
        self.redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        self.event_bus = EventBus(self.redis_client)
        self.temp_dir = tempfile.mkdtemp()

    def __del__(self):
        """Clean up temporary directory on worker deletion."""
        import shutil

        try:
            shutil.rmtree(self.temp_dir, ignore_errors=True)
        except Exception as e:
            logger.debug(f"Ignored error: {e}")

    async def _docling_convert_async(self, document_bytes: bytes, filename: str) -> Dict[str, Any]:
        """Submit document to Docling async endpoint and poll until completion.

        Uses aiohttp for non-blocking HTTP calls, allowing other coroutines
        (other documents) to run while waiting for Docling polls.

        Implements async long-polling: the ?wait= parameter keeps the server
        connection open for up to N seconds before returning, reducing poll
        frequency. A 2s sleep between polls guards against immediate returns
        (e.g., task still queued on the Docling side).

        Args:
            document_bytes: Raw binary content of document.
            filename: Filename for Docling processing (determines file type).

        Returns:
            Dictionary with Docling result:
                document (dict): Extracted document with md_content/text_content.
                pages (list): List of extracted pages (if applicable).
                (other fields): Task metadata.

        Raises:
            TimeoutError: If conversion exceeds DOCLING_CONVERSION_TIMEOUT (1800s default).
            RuntimeError: If Docling returns failure/error status.
            aiohttp.ClientError: On HTTP errors or connection failures.
        """
        async with aiohttp.ClientSession() as session:
            # Step 1: Submit conversion job
            form_data = aiohttp.FormData()
            form_data.add_field(
                "files",
                document_bytes,
                filename=filename,
                content_type="application/octet-stream",
            )
            form_data.add_field("do_ocr", str(DOCLING_DO_OCR).lower())
            form_data.add_field("ocr_engine", DOCLING_OCR_ENGINE)
            # Use placeholder instead of embedded base64 to keep output size
            # manageable. Embedded images inflate markdown to tens of MB,
            # producing thousands of unnecessary chunks downstream.
            form_data.add_field("image_export_mode", "placeholder")

            async with session.post(
                f"{DOCLING_URL}/v1/convert/file/async",
                data=form_data,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                resp.raise_for_status()
                task_id = (await resp.json())["task_id"]

            logger.info(f"Docling async task submitted: {task_id}")

            # Step 2: Poll until task_status is 'success' or 'failure'.
            # Use docling's long-poll ?wait= param so the server holds the
            # connection up to N seconds before returning, reducing polling
            # frequency. A 2s sleep between iterations guards against
            # tight-looping if docling returns immediately (e.g. still queued).
            poll_deadline = asyncio.get_event_loop().time() + DOCLING_CONVERSION_TIMEOUT
            while True:
                remaining = poll_deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    raise TimeoutError(
                        f"Docling conversion timed out after {DOCLING_CONVERSION_TIMEOUT}s"
                        f" (task_id={task_id})"
                    )

                wait_secs = min(30, remaining)
                async with session.get(
                    f"{DOCLING_URL}/v1/status/poll/{task_id}",
                    params={"wait": int(wait_secs)},
                    timeout=aiohttp.ClientTimeout(total=wait_secs + 10),
                ) as resp:
                    resp.raise_for_status()
                    status_data = await resp.json()

                task_status = status_data.get("task_status", "")
                logger.info(f"Docling task {task_id} status: {task_status}")

                if task_status == "success":
                    break
                if task_status in ("failure", "error"):
                    raise RuntimeError(
                        f"Docling conversion failed (task_id={task_id}): {status_data}"
                    )
                # Still pending/processing — yield control to other coroutines
                await asyncio.sleep(2)

            # Step 3: Fetch the result
            async with session.get(
                f"{DOCLING_URL}/v1/result/{task_id}",
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                resp.raise_for_status()
                return await resp.json()

    async def extract_text_from_base64(
        self, document_base64: str, filename: str = "document"
    ) -> Dict[str, Any]:
        """Extract text from base64-encoded document.

        Decodes base64 content and passes to Docling. Auto-detects PDF format
        from magic bytes if filename doesn't have .pdf extension.

        Args:
            document_base64: Base64-encoded document bytes.
            filename: Original filename (used for type detection).

        Returns:
            Dictionary with:
                text (str): Extracted text (markdown or plain).
                metadata (dict): Extraction metadata (pages, method="base64").

        Raises:
            ValueError: If base64 decoding fails.
            TimeoutError: If Docling conversion times out.
            RuntimeError: If Docling fails.
        """
        try:
            document_bytes = base64.b64decode(document_base64)
            logger.info(f"Decoded {len(document_bytes)} bytes, filename: {filename}")

            # Detect PDF by magic bytes
            if document_bytes[:4] == b"%PDF":
                if not filename.lower().endswith(".pdf"):
                    filename = filename + ".pdf"
                    logger.info(f"Adjusted filename to: {filename}")

            result = await self._docling_convert_async(document_bytes, filename)
            doc = result.get("document", {})
            text = doc.get("md_content") or doc.get("text_content") or ""

            metadata = {
                "docling_pages": doc.get("pages") and len(doc.get("pages", [])),
                "extraction_method": "base64",
            }

            return {"text": text, "metadata": metadata}

        except Exception as e:
            import traceback

            logger.error(f"Traceback: {traceback.format_exc()}")
            logger.error(f"Failed to extract text from base64: {e}")
            raise

    async def extract_text_from_file(
        self, file_path: str, filename: str = "document"
    ) -> Dict[str, Any]:
        """Extract text from document file on disk.

        Reads file from disk and passes to Docling for text extraction.

        Args:
            file_path: Absolute path to document file.
            filename: Filename for Docling (may differ from path basename).

        Returns:
            Dictionary with:
                text (str): Extracted text (markdown or plain).
                metadata (dict): Extraction metadata (pages, method="file").

        Raises:
            FileNotFoundError: If file_path doesn't exist.
            TimeoutError: If Docling conversion times out.
            RuntimeError: If Docling fails.
        """
        try:
            # File I/O is fast (local disk) — acceptable brief sync call
            with open(file_path, "rb") as f:
                document_bytes = f.read()
            logger.info(f"Read {len(document_bytes)} bytes from {file_path}")

            result = await self._docling_convert_async(document_bytes, filename)
            doc = result.get("document", {})
            text = doc.get("md_content") or doc.get("text_content") or ""

            metadata = {
                "docling_pages": doc.get("pages") and len(doc.get("pages", [])),
                "extraction_method": "file",
            }

            return {"text": text, "metadata": metadata}

        except Exception as e:
            import traceback

            logger.error(f"Traceback: {traceback.format_exc()}")
            logger.error(f"Failed to extract text from file: {e}")
            raise

    async def extract_text_from_url(self, document_url: str) -> Dict[str, Any]:
        """Extract text from document at URL.

        Handles internal docling: URLs with path traversal protection, and
        remote HTTP(S) URLs. Internal URLs are resolved to local paths and
        read from disk; external URLs are fetched via aiohttp (non-blocking).

        Args:
            document_url: URL to document (docling:// or http(s)://).

        Returns:
            Dictionary with:
                text (str): Extracted text (markdown or plain).
                metadata (dict): Extraction metadata (pages, method="url").

        Raises:
            ValueError: If path traversal attempted or URL is invalid.
            aiohttp.ClientError: If async HTTP fetch fails.
            TimeoutError: If Docling conversion times out.
            RuntimeError: If Docling fails.

        Note:
            Path traversal is validated by ensuring resolved path stays within
            /app directory tree. Prevents accessing files outside container.
        """
        try:
            if document_url.startswith("http://docling:") or document_url.startswith(
                "http://docling/"
            ):
                if "://docling:" in document_url:
                    url_path = document_url.split("/data/uploads/")[-1]
                    local_path = Path("/app/data/uploads") / url_path
                elif "/app/" in document_url:
                    url_path = document_url.split("/app/")[-1]
                    local_path = Path("/app") / url_path
                else:
                    url_path = document_url.split("/data/uploads/")[-1]
                    local_path = Path("/app/data/uploads") / url_path

                # Validate that resolved path stays within allowed directory
                try:
                    resolved_path = local_path.resolve()
                    allowed_base = Path("/app").resolve()
                    resolved_path.relative_to(allowed_base)
                except ValueError:
                    raise ValueError(f"Path traversal attempt detected: {local_path}")

                # Local file read — fast, sync is fine
                with open(resolved_path, "rb") as f:
                    document_bytes = f.read()

                filename = resolved_path.name
            else:
                # External URL — fetch with aiohttp (non-blocking)
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        document_url,
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as resp:
                        resp.raise_for_status()
                        document_bytes = await resp.read()

                filename = document_url.split("/")[-1]
                if "." not in filename:
                    filename = "document.pdf"

            result = await self._docling_convert_async(document_bytes, filename)
            doc = result.get("document", {})
            text = doc.get("md_content") or doc.get("text_content") or ""

            metadata = {
                "docling_pages": doc.get("pages") and len(doc.get("pages", [])),
                "extraction_method": "url",
            }

            return {"text": text, "metadata": metadata}

        except Exception as e:
            import traceback

            logger.error(f"Traceback: {traceback.format_exc()}")
            logger.error(f"Failed to extract text from URL: {e}")
            raise

    async def _process_message_async(
        self,
        message: aio_pika.abc.AbstractIncomingMessage,
        channel: aio_pika.abc.AbstractChannel,
    ) -> None:
        """Process a single extraction job message asynchronously.

        Main message handler that orchestrates the full extraction pipeline:
        1. Parse job message (JSON with document_path, document_base64, or document_url)
        2. Extract text via Docling (async polling — non-blocking)
        3. Extract document metadata (author, creation date, page count, etc.)
        4. Analyze text (language, readability, patterns)
        5. Chunk text into overlapping token-based segments
        6. Classify document source type
        7. Store all results to Redis (sync — fast I/O)
        8. Route to downstream queues (embeddings, entities, metadata) via aio_pika
        9. Publish job progress update

        Input message format (JSON):
            {
                "job_id": str,              # Unique job identifier
                "document_path": str,       # Optional: absolute file path
                "document_base64": str,     # Optional: base64-encoded content
                "document_url": str,        # Optional: URL to document
                "filename": str,            # Optional: original filename
                "mime_type": str,           # Optional: declared MIME type
                "entity_types": [str],      # Optional: entity types to extract
            }

        Either document_path, document_base64, or document_url must be present.

        Redis storage (keys created):
            orchestrator:job:{job_id}:status -> hash with status field
            orchestrator:job:{job_id}:text -> full extracted text
            orchestrator:job:{job_id}:chunks -> JSON array of chunks
            orchestrator:job:{job_id}:metadata:document -> document metadata JSON
            orchestrator:job:{job_id}:metadata:text -> text analytics JSON
            orchestrator:job:{job_id}:source_classification -> classification result
            orchestrator:job:{job_id}:steps -> hash with extraction="completed"

        Queue publishing:
            - Publishes to "embeddings" queue (all non-spreadsheet documents)
            - Publishes to "entities" queue (all documents)
            - Publishes to "metadata" queue (non-spreadsheet documents)
            - Spreadsheets (csv, xls, xlsx) route to "entities" only

        Message published to queues contains:
            {
                "job_id": str,
                "chunks": [chunk dicts],
                "document_metadata": metadata dict,
                "entity_types": [str],          # Optional, if provided
            }

        Error handling:
            - Docling timeouts: TimeoutError, job marked as failed, message nacked
            - Empty text: ValueError, MIME type mismatch details logged
            - Source classification: Logged as warning, doesn't fail job
            - exiftool missing: Gracefully degraded, metadata partially filled
            - Any exception: Job status set to "failed", error stored to Redis,
              published to failed queue, message nacked (not requeued)

        The aio_pika message.process() context manager handles ack/nack:
            - Normal exit: message is acked
            - Exception raised: message is nacked with requeue=False

        Metrics recorded:
            - extraction_worker_jobs_total (counter): success/error labels
            - extraction_worker_job_duration_seconds (histogram): job duration
        """
        job_id = None
        temp_file_path = None
        start_time = time.time()

        # message.process() auto-acks on clean exit, nacks on exception
        async with message.process(requeue=False):
            try:
                body = json.loads(message.body)
                job_id = body.get("job_id")

                logger.info(f"Processing text extraction for job: {job_id}")

                self.redis_client.hset(f"orchestrator:job:{job_id}:status", "status", "extracting")

                if body.get("document_path"):
                    result = await self.extract_text_from_file(
                        body["document_path"], os.path.basename(body["document_path"])
                    )
                    text = result["text"]
                elif body.get("document_base64"):
                    result = await self.extract_text_from_base64(
                        body["document_base64"],
                        filename=body.get("filename", "document"),
                    )
                    text = result["text"]
                elif body.get("document_url"):
                    result = await self.extract_text_from_url(body["document_url"])
                    text = result["text"]
                else:
                    raise ValueError("No document provided")

                # For metadata extraction, use original file if available
                if body.get("document_path"):
                    temp_file_path = body["document_path"]
                else:
                    # Derive file extension from message filename or mime_type
                    filename = body.get("filename", "document")
                    file_ext = Path(filename).suffix or ".bin"
                    temp_fd, temp_file_path = tempfile.mkstemp(suffix=file_ext)
                    try:
                        os.write(temp_fd, base64.b64decode(body.get("document_base64", "")))
                    finally:
                        os.close(temp_fd)

                document_metadata = extract_pdf_metadata(
                    temp_file_path,
                    os.path.basename(body.get("document_path", "document.pdf")),
                )

                # Guard: fail if Docling returned empty text (likely file type mismatch)
                if not text:
                    actual_mime = document_metadata.get("mime_type", "unknown")
                    declared_mime = body.get("mime_type", "unknown")
                    raise ValueError(
                        f"Empty text extracted from document. "
                        f"Actual MIME type: {actual_mime}, declared: {declared_mime}. "
                        f"File may be corrupt or have wrong extension."
                    )

                text_metadata = analyze_text(text)

                chunks = chunk_text(text)

                self.redis_client.hset(
                    f"orchestrator:job:{job_id}:status", "status", "processing"
                )

                text_ref = STORE.put(text.encode("utf-8"))
                self.redis_client.set(f"orchestrator:job:{job_id}:text", text_ref)
                chunks_ref = STORE.put(json.dumps(chunks).encode("utf-8"))
                self.redis_client.set(f"orchestrator:job:{job_id}:chunks", chunks_ref)
                self.redis_client.set(
                    f"orchestrator:job:{job_id}:metadata:document",
                    json.dumps(document_metadata),
                )
                self.redis_client.set(
                    f"orchestrator:job:{job_id}:metadata:text", json.dumps(text_metadata)
                )

                self.redis_client.hset(
                    f"orchestrator:job:{job_id}:steps", "extraction", "completed"
                )

                # Classify document source
                try:
                    classification = SourceClassifier.classify(text)
                    if classification:
                        self.redis_client.set(
                            f"orchestrator:job:{job_id}:source_classification",
                            json.dumps(classification),
                        )
                        logger.info(
                            f"Document classified as: {classification['document_type']} "
                            f"(confidence={classification['confidence']:.2f})"
                        )
                except Exception as e:
                    logger.warning(f"Source classification failed: {e}")
                    # Continue anyway - classification is optional

                logger.info(
                    f"Stored for job {job_id}: text={len(text)} chars, chunks={len(chunks)}, "
                    f"doc_metadata keys={list(document_metadata.keys())}"
                )

                job_message = {
                    "job_id": job_id,
                    "chunks": chunks,
                    "document_metadata": document_metadata,
                    "pipeline_version": body.get("pipeline_version", "v1"),
                }

                if body.get("entity_types"):
                    job_message["entity_types"] = body["entity_types"]
                if body.get("features"):
                    job_message["features"] = body["features"]

                # Determine if this is a spreadsheet (reduce pipeline: entities only)
                is_spreadsheet = False
                if body.get("mime_type") == "application/spreadsheet":
                    is_spreadsheet = True
                elif body.get("document_path"):
                    path_lower = body["document_path"].lower()
                    if path_lower.endswith((".csv", ".xls", ".xlsx")):
                        is_spreadsheet = True

                # Route to appropriate queues
                features = body.get("features") or []
                if is_spreadsheet:
                    target_queues = ["entities"]
                    logger.info(f"Detected spreadsheet, routing to entities-only pipeline")
                else:
                    target_queues = ["embeddings", "entities", "metadata"]

                if "inferences" in features:
                    target_queues.append("inferences")

                job_message_json = json.dumps(job_message).encode()
                for queue_name in target_queues:
                    await channel.default_exchange.publish(
                        aio_pika.Message(
                            body=job_message_json,
                            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                            content_type="application/json",
                        ),
                        routing_key=queue_name,
                    )
                    logger.info(f"Published job {job_id} to queue: {queue_name}")

                self.event_bus.publish_job_progress(job_id, 25, "processing")

                logger.info(f"Text extraction completed for job: {job_id}")

                # Record metrics
                job_duration.observe(time.time() - start_time)
                jobs_total.labels(status="success").inc()

            except Exception as e:
                logger.error(f"Error processing extraction: {e}")
                if job_id:
                    self.redis_client.hset(
                        f"orchestrator:job:{job_id}:status", "status", "failed"
                    )
                    self.redis_client.set(f"orchestrator:job:{job_id}:error", str(e))
                    self.event_bus.publish_job_failed(job_id, str(e))

                # Record failure metrics
                jobs_total.labels(status="error").inc()

                raise  # Re-raise so message.process() will nack the message
            finally:
                if temp_file_path and os.path.exists(temp_file_path):
                    try:
                        os.remove(temp_file_path)
                    except Exception as e:
                        logger.debug(f"Ignored error cleaning up temp file: {e}")


async def _run() -> None:
    """Async main loop for the extraction worker.

    Connects to RabbitMQ using aio_pika robust connection (auto-reconnect).
    Dispatches each incoming message as an independent asyncio task, allowing
    up to PREFETCH_COUNT Docling jobs to run concurrently.

    Graceful shutdown:
        - SIGINT / SIGTERM sets a stop_event
        - The queue iterator loop breaks on stop_event
        - Pending tasks are awaited before exit (up to their natural completion)
    """
    worker = ExtractionWorker()

    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()

    def _handle_signal() -> None:
        logger.info("Shutdown signal received, finishing current messages...")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    start_http_server(METRICS_PORT)
    logger.info(f"Metrics server started on port {METRICS_PORT}")

    pending_tasks: set = set()

    while not stop_event.is_set():
        try:
            connection = await aio_pika.connect_robust(RABBITMQ_URL)
            async with connection:
                channel = await connection.channel()
                await channel.set_qos(prefetch_count=PREFETCH_COUNT)

                queue = await declare_queue_async(channel, QUEUE_NAME)
                logger.info(
                    f"Consuming from queue: {QUEUE_NAME} "
                    f"(concurrency={PREFETCH_COUNT})"
                )

                async with queue.iterator() as q_iter:
                    async for message in q_iter:
                        if stop_event.is_set():
                            break

                        # Dispatch concurrently — each Docling poll yields the
                        # event loop so other tasks progress in parallel.
                        task = asyncio.create_task(
                            worker._process_message_async(message, channel)
                        )
                        pending_tasks.add(task)
                        task.add_done_callback(pending_tasks.discard)

        except Exception as e:
            if stop_event.is_set():
                break
            logger.error(f"RabbitMQ connection error: {e}. Reconnecting in 5s...")
            await asyncio.sleep(5)

    # Wait for any in-flight tasks to complete before exiting
    if pending_tasks:
        logger.info(f"Waiting for {len(pending_tasks)} in-flight task(s) to complete...")
        await asyncio.gather(*pending_tasks, return_exceptions=True)

    logger.info("Extraction worker shutdown complete")


def main() -> None:
    """Main entry point for extraction worker.

    Initialises the async event loop and runs _run() until termination signal.
    """
    logger.info("Starting Extraction Worker")
    asyncio.run(_run())


if __name__ == "__main__":
    main()
