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

Environment variables:
    REDIS_URL: Redis connection URL (default: redis://localhost:6379)
    RABBITMQ_URL: RabbitMQ connection URL (default: amqp://localhost:5672/)
    DOCLING_URL: Docling API endpoint (default: http://docling:5001)
    QUEUE_NAME: Input queue for extraction jobs (default: extract_text)
    PREFETCH_COUNT: Max jobs to prefetch from queue (default: 3)
    METRICS_PORT: Prometheus metrics port (default: 8004)
    CHUNK_SIZE_TOKENS: Tokens per chunk (default: 512)
    CHUNK_OVERLAP_TOKENS: Overlap between chunks (default: 50)
    EXIFTOOL_PATH: Path to exiftool binary (default: /usr/bin/exiftool)
    DOCLING_DO_OCR: Enable OCR for text-based PDFs (default: false)
    DOCLING_OCR_ENGINE: OCR engine to use (default: rapidocr)
    DOCLING_CONVERSION_TIMEOUT: Max seconds for Docling conversion (default: 1800)
"""

import os
import signal
import sys
import json
import base64
import hashlib
import subprocess
import tempfile
import logging
import time
import pika
import redis
import requests
import magic
import langdetect
import textstat
from typing import Dict, Optional, List, Any
from urllib.parse import urlparse
from pathlib import Path
import tiktoken
from prometheus_client import Counter, Histogram, start_http_server

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
from pkg.events_python import EventBus
from pkg.worker_common.rabbitmq import (
    parse_rabbitmq_url,
    connect_rabbitmq,
    declare_queue,
)

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
PREFETCH_COUNT = int(os.getenv("PREFETCH_COUNT", "3"))
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
            chunk_text = tokenizer.decode(chunk_tokens)

            chunks.append(
                {
                    "chunk_id": f"chunk_{chunk_num:03d}",
                    "text": chunk_text,
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
            chunk_text = "".join(chunk_chars)

            chunks.append(
                {
                    "chunk_id": f"chunk_{chunk_num:03d}",
                    "text": chunk_text,
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
    """RabbitMQ consumer for document extraction and metadata pipeline.

    Processes documents from the extraction queue by:
    1. Downloading documents (file path, base64, or URL)
    2. Extracting text via Docling API with async polling
    3. Extracting document metadata (author, creation date, page count, etc.)
    4. Analyzing text for language, readability, and patterns
    5. Chunking text into overlapping token-based segments
    6. Classifying document source type (notariado, catastro, etc.)
    7. Storing results to Redis
    8. Publishing job to downstream queues (embeddings, entities, metadata)

    This is the first step in the document processing pipeline. Results are stored
    in Redis under keys like orchestrator:job:{job_id}:{text,chunks,metadata:document}.

    Attributes:
        redis_client: Redis connection for result storage.
        event_bus: EventBus instance for job progress updates.
        temp_dir: Temporary directory for storing intermediate files.
    """

    def __init__(self):
        """Initialize extraction worker with Redis and RabbitMQ clients.

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
        except:
            pass

    def _docling_convert(self, document_bytes: bytes, filename: str) -> Dict[str, Any]:
        """Submit document to Docling async endpoint and poll until completion.

        Implements async conversion with long-polling to avoid uvicorn's per-request
        timeout (~120s), which is insufficient for large PDFs on CPU. Uses exponential
        backoff (5-30s) between polls.

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
            requests.RequestException: On HTTP errors or connection failures.

        Note:
            Uses Docling's ?wait= parameter for server-side long-polling to reduce
            API call frequency. Minimum 5s sleep between polls to prevent busy-waiting.
        """
        # Submit conversion job asynchronously
        submit_resp = requests.post(
            f"{DOCLING_URL}/v1/convert/file/async",
            files={"files": (filename, document_bytes)},
            data={
                "do_ocr": str(DOCLING_DO_OCR).lower(),
                "ocr_engine": DOCLING_OCR_ENGINE,
                # Use placeholder instead of embedded base64 to keep output size
                # manageable. Embedded images inflate markdown to tens of MB,
                # producing thousands of unnecessary chunks downstream.
                "image_export_mode": "placeholder",
            },
            timeout=30,
        )
        submit_resp.raise_for_status()
        task_id = submit_resp.json()["task_id"]
        logger.info(f"Docling async task submitted: {task_id}")

        # Poll until task_status is 'success' or 'failure'.
        # Use docling's long-poll ?wait= param so the server holds the connection
        # up to N seconds before returning, reducing polling frequency.
        # A minimum sleep of 5 s between iterations guards against tight-looping
        # if docling returns immediately (e.g. task still queued).
        poll_deadline = time.time() + DOCLING_CONVERSION_TIMEOUT
        while True:
            remaining = poll_deadline - time.time()
            if remaining <= 0:
                raise TimeoutError(
                    f"Docling conversion timed out after {DOCLING_CONVERSION_TIMEOUT}s"
                    f" (task_id={task_id})"
                )
            wait_secs = min(30, remaining)
            status_resp = requests.get(
                f"{DOCLING_URL}/v1/status/poll/{task_id}",
                params={"wait": wait_secs},
                timeout=wait_secs + 10,
            )
            status_resp.raise_for_status()
            status_data = status_resp.json()
            task_status = status_data.get("task_status", "")
            logger.info(f"Docling task {task_id} status: {task_status}")

            if task_status == "success":
                break
            if task_status in ("failure", "error"):
                raise RuntimeError(f"Docling conversion failed (task_id={task_id}): {status_data}")
            # Still pending/processing — sleep before next poll to avoid spinning
            time.sleep(5)

        # Fetch the result
        result_resp = requests.get(
            f"{DOCLING_URL}/v1/result/{task_id}",
            timeout=30,
        )
        result_resp.raise_for_status()
        return result_resp.json()

    def extract_text_from_base64(
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

            result = self._docling_convert(document_bytes, filename)
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

    def extract_text_from_file(self, file_path: str, filename: str = "document") -> Dict[str, Any]:
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
            with open(file_path, "rb") as f:
                document_bytes = f.read()
            logger.info(f"Read {len(document_bytes)} bytes from {file_path}")

            result = self._docling_convert(document_bytes, filename)
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

    def extract_text_from_url(self, document_url: str) -> Dict[str, Any]:
        """Extract text from document at URL.

        Handles internal docling: URLs with path traversal protection, and
        remote HTTP(S) URLs. Internal URLs are resolved to local paths and
        read from disk; external URLs are fetched via HTTP.

        Args:
            document_url: URL to document (docling:// or http(s)://).

        Returns:
            Dictionary with:
                text (str): Extracted text (markdown or plain).
                metadata (dict): Extraction metadata (pages, method="url").

        Raises:
            ValueError: If path traversal attempted or URL is invalid.
            requests.RequestException: If HTTP fetch fails.
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

                with open(resolved_path, "rb") as f:
                    document_bytes = f.read()

                filename = resolved_path.name
            else:
                response = requests.get(document_url, timeout=30)
                response.raise_for_status()
                document_bytes = response.content

                filename = document_url.split("/")[-1]
                if "." not in filename:
                    filename = "document.pdf"

            result = self._docling_convert(document_bytes, filename)
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

    def process_message(self, ch, method, properties, body):
        """Process a single extraction job from RabbitMQ queue.

        Main message handler that orchestrates the full extraction pipeline:
        1. Parse job message (JSON with document_path, document_base64, or document_url)
        2. Extract text via Docling (async polling)
        3. Extract document metadata (author, creation date, page count, etc.)
        4. Analyze text (language, readability, patterns)
        5. Chunk text into overlapping token-based segments
        6. Classify document source type
        7. Store all results to Redis
        8. Route to downstream queues (embeddings, entities, metadata)
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
            - Docling timeouts: Raises TimeoutError, job marked as failed
            - Empty text: Raises ValueError, includes MIME type mismatch details
            - Source classification: Logged as warning, doesn't fail job
            - exiftool missing: Gracefully degraded, metadata partially filled
            - Any exception: Job status set to "failed", error stored to Redis,
              published to failed queue, message nacked (not requeued)

        Metrics recorded:
            - extraction_worker_jobs_total (counter): success/error labels
            - extraction_worker_job_duration_seconds (histogram): job duration

        Temporary files:
            - Base64 documents: Created in temp directory, cleaned up in finally
            - File paths: Used directly, not copied

        Raises:
            Never raises exceptions. All errors are caught, logged, and converted
            to failed job status in Redis. This allows RabbitMQ to move on to
            next job without requeuing.
        """
        job_id = None
        temp_file_path = None
        start_time = time.time()

        try:
            message = json.loads(body)
            job_id = message.get("job_id")

            logger.info(f"Processing text extraction for job: {job_id}")

            self.redis_client.hset(f"orchestrator:job:{job_id}:status", "status", "extracting")

            if message.get("document_path"):
                result = self.extract_text_from_file(
                    message["document_path"], os.path.basename(message["document_path"])
                )
                text = result["text"]
            elif message.get("document_base64"):
                result = self.extract_text_from_base64(
                    message["document_base64"],
                    filename=message.get("filename", "document"),
                )
                text = result["text"]
            elif message.get("document_url"):
                result = self.extract_text_from_url(message["document_url"])
                text = result["text"]
            else:
                raise ValueError("No document provided")

            # For metadata extraction, use original file if available
            if message.get("document_path"):
                temp_file_path = message["document_path"]
            else:
                # Derive file extension from message filename or mime_type
                filename = message.get("filename", "document")
                file_ext = Path(filename).suffix or ".bin"
                temp_fd, temp_file_path = tempfile.mkstemp(suffix=file_ext)
                try:
                    os.write(temp_fd, base64.b64decode(message.get("document_base64", "")))
                finally:
                    os.close(temp_fd)

            document_metadata = extract_pdf_metadata(
                temp_file_path,
                os.path.basename(message.get("document_path", "document.pdf")),
            )

            # Guard: fail if Docling returned empty text (likely file type mismatch)
            if not text:
                actual_mime = document_metadata.get("mime_type", "unknown")
                declared_mime = message.get("mime_type", "unknown")
                raise ValueError(
                    f"Empty text extracted from document. "
                    f"Actual MIME type: {actual_mime}, declared: {declared_mime}. "
                    f"File may be corrupt or have wrong extension."
                )

            text_metadata = analyze_text(text)

            chunks = chunk_text(text)

            self.redis_client.hset(f"orchestrator:job:{job_id}:status", "status", "processing")

            self.redis_client.set(f"orchestrator:job:{job_id}:text", text)
            self.redis_client.set(f"orchestrator:job:{job_id}:chunks", json.dumps(chunks))
            self.redis_client.set(
                f"orchestrator:job:{job_id}:metadata:document",
                json.dumps(document_metadata),
            )
            self.redis_client.set(
                f"orchestrator:job:{job_id}:metadata:text", json.dumps(text_metadata)
            )

            self.redis_client.hset(f"orchestrator:job:{job_id}:steps", "extraction", "completed")

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
                f"Stored for job {job_id}: text={len(text)} chars, chunks={len(chunks)}, doc_metadata keys={list(document_metadata.keys())}"
            )

            params = parse_rabbitmq_url(RABBITMQ_URL)
            connection = pika.BlockingConnection(params)
            channel = connection.channel()

            job_message = {
                "job_id": job_id,
                "chunks": chunks,
                "document_metadata": document_metadata,
            }

            if message.get("entity_types"):
                job_message["entity_types"] = message["entity_types"]

            # Determine if this is a spreadsheet (reduce pipeline: entities only)
            is_spreadsheet = False
            if message.get("mime_type") == "application/spreadsheet":
                is_spreadsheet = True
            elif message.get("document_path"):
                path_lower = message["document_path"].lower()
                if path_lower.endswith((".csv", ".xls", ".xlsx")):
                    is_spreadsheet = True

            # Route to appropriate queues
            if is_spreadsheet:
                target_queues = ["entities"]
                logger.info(f"Detected spreadsheet, routing to entities-only pipeline")
            else:
                target_queues = ["embeddings", "entities", "metadata"]

            job_message_json = json.dumps(job_message)
            for queue in target_queues:
                channel.basic_publish(
                    exchange="",
                    routing_key=queue,
                    body=job_message_json,
                    properties=pika.BasicProperties(
                        delivery_mode=2,
                        content_type="application/json",
                    ),
                )
                logger.info(f"Published job {job_id} to queue: {queue}")

            connection.close()

            self.event_bus.publish_job_progress(job_id, 25, "processing")

            ch.basic_ack(delivery_tag=method.delivery_tag)
            logger.info(f"Text extraction completed for job: {job_id}")

            # Record metrics
            job_duration.observe(time.time() - start_time)
            jobs_total.labels(status="success").inc()

        except Exception as e:
            logger.error(f"Error processing extraction: {e}")
            if job_id:
                self.redis_client.hset(f"orchestrator:job:{job_id}:status", "status", "failed")
                self.redis_client.set(f"orchestrator:job:{job_id}:error", str(e))
                self.event_bus.publish_job_failed(job_id, str(e))
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

            # Record failure metrics
            jobs_total.labels(status="error").inc()
        finally:
            if temp_file_path and os.path.exists(temp_file_path):
                try:
                    os.remove(temp_file_path)
                except:
                    pass


def signal_handler(signum, frame):
    """Handle termination signals (SIGTERM, SIGINT) gracefully.

    Logs shutdown notice and exits cleanly, allowing RabbitMQ connection to close
    and temporary files to be cleaned up by ExtractionWorker.__del__.

    Args:
        signum: Signal number (SIGINT=2, SIGTERM=15).
        frame: Current stack frame.
    """
    logger.info("Received shutdown signal, stopping worker...")
    sys.exit(0)


def main():
    """Main entry point for extraction worker.

    Initializes infrastructure and starts consuming from RabbitMQ extraction queue.
    Sets up signal handlers for graceful shutdown (SIGINT/SIGTERM) and Prometheus
    metrics endpoint. Implements auto-reconnect with 5-second backoff on connection
    failures.

    Metrics endpoint:
        Prometheus metrics available at http://localhost:METRICS_PORT/metrics
        Tracks:
            - extraction_worker_jobs_total (counter): Jobs by status (success/error)
            - extraction_worker_job_duration_seconds (histogram): Job duration

    Queue configuration:
        Input queue: QUEUE_NAME (extract_text by default)
        Prefetch count: PREFETCH_COUNT (3 by default) - max jobs to prefetch from queue

    Error recovery:
        If RabbitMQ connection fails, waits 5 seconds and retries. Continues
        indefinitely until signal received.
    """
    logger.info("Starting Extraction Worker")

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    start_http_server(METRICS_PORT)
    logger.info(f"Metrics server started on port {METRICS_PORT}")

    worker = ExtractionWorker()

    while True:
        try:
            with connect_rabbitmq(RABBITMQ_URL, prefetch_count=PREFETCH_COUNT) as (
                connection,
                channel,
            ):
                declare_queue(channel, QUEUE_NAME)
                logger.info(f"Consuming from queue: {QUEUE_NAME}")
                channel.basic_consume(
                    queue=QUEUE_NAME,
                    on_message_callback=worker.process_message,
                    auto_ack=False,
                )
                channel.start_consuming()
        except Exception as e:
            logger.error(f"RabbitMQ connection error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
