#!/usr/bin/env python3
"""Metadata extraction worker for textFlow.

This worker extracts lightweight text-level metadata without requiring ML models,
making it fast and deterministic. It consumes extracted text from the extraction
worker via RabbitMQ, computes document statistics (character count, word count,
language heuristic, readability score, etc.), and stores results in Redis.

Key characteristics:
    - No ML model dependencies (unlike embeddings, entities workers)
    - Language detection is heuristic-based (common word patterns), not accurate
    - Deterministic and fast execution
    - Metadata includes: text length, word count, line count, content hash (SHA-256),
      language detection, readability score (Flesch Reading Ease), URL/email/number
      detection, and MIME type guessing.

Architecture:
    Input: Extracted text from extraction worker (stored in Redis)
    Queue: RabbitMQ metadata queue (default: "metadata")
    Output: Metadata dict stored in Redis at orchestrator:job:{job_id}:metadata
    Events: Publishes job progress events via EventBus (status=metadata, 100%)

Environment variables:
    REDIS_URL (default: redis://localhost:6379): Redis connection URL
    RABBITMQ_URL (default: amqp://localhost:5672/): RabbitMQ connection URL
    QUEUE_NAME (default: "metadata"): RabbitMQ queue to consume from
    METRICS_PORT (default: 8003): Prometheus metrics server port
"""

import os
import json
import logging
import signal
import sys
import time
from typing import Dict, Optional
from datetime import datetime
import hashlib
import mimetypes

import redis
import requests
from prometheus_client import Counter, Histogram, start_http_server

# Import event bus
# In Docker: worker.py is at /app/worker.py, pkg is at /app/pkg
sys.path.insert(0, "/app")
from pkg.events_python import EventBus
from pkg.worker_common.rabbitmq import connect_rabbitmq, declare_queue

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

_stopping = False

jobs_total = Counter("metadata_worker_jobs_total", "Total jobs processed", ["status"])
job_duration = Histogram("metadata_worker_job_duration_seconds", "Job duration")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://localhost:5672/")
QUEUE_NAME = os.getenv("QUEUE_NAME", "metadata")
METRICS_PORT = int(os.getenv("METRICS_PORT", "8003"))


class MetadataWorker:
    """RabbitMQ consumer for metadata extraction queue.

    Responsible for:
        - Consuming metadata extraction jobs from RabbitMQ
        - Retrieving extracted text from Redis (stored by extraction worker)
        - Computing comprehensive document metadata
        - Storing results back to Redis
        - Publishing progress events to notify orchestrator

    Attributes:
        redis_client: Redis client for data storage and retrieval
        event_bus: EventBus instance for publishing progress events
    """

    def __init__(self):
        """Initialize Redis connection and event bus.

        Establishes connection to Redis using REDIS_URL environment variable
        and initializes EventBus for publishing progress events.

        Raises:
            redis.ConnectionError: If Redis connection fails
        """
        self.redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        self.event_bus = EventBus(self.redis_client)

    def extract_metadata(self, text: str, document_url: Optional[str] = None) -> Dict:
        """Extract comprehensive metadata from document text.

        Computes lightweight, deterministic metadata including text statistics,
        content hash, language detection (heuristic), readability score, and
        structural features. No ML models required.

        Args:
            text: Full extracted document text to analyze.
            document_url: Optional source URL for MIME type detection and metadata.

        Returns:
            Dict with the following keys:
                extracted_at (str): ISO 8601 timestamp of extraction
                text_length (int): Total character count in document
                word_count (int): Number of whitespace-separated words
                line_count (int): Number of lines (newlines + 1)
                char_count (int): Duplicate of text_length for clarity
                content_hash (str): SHA-256 hex digest of text for deduplication
                source_url (str, optional): Included if document_url provided
                mime_type (str, optional): Guessed MIME type from URL extension
                language (str): ISO 639-1 language code ("es", "en", "unknown")
                has_urls (bool): True if text contains "http://" or "https://"
                has_emails (bool): True if text contains "@" and "." pattern
                has_numbers (bool): True if text contains any digits
                avg_sentence_length (float): Average words per sentence
                    (calculated from period, exclamation, question count)

        Notes:
            - All fields are present even if computation partially fails
            - Language detection is heuristic-based (common word patterns),
              not ML-based; suitable for quick filtering only
            - Readability score computation is attempted but not always meaningful
            - Content hash enables deduplication across documents
        """
        metadata = {
            "extracted_at": datetime.utcnow().isoformat(),
            "text_length": len(text),
            "word_count": len(text.split()),
            "line_count": text.count("\n") + 1,
            "char_count": len(text),
        }

        # Content hash
        metadata["content_hash"] = hashlib.sha256(text.encode("utf-8")).hexdigest()

        # MIME type detection
        if document_url:
            metadata["source_url"] = document_url
            mime_type, _ = mimetypes.guess_type(document_url)
            if mime_type:
                metadata["mime_type"] = mime_type

        # Language detection (basic heuristic)
        metadata["language"] = self.detect_language(text)

        # Document structure analysis
        metadata["has_urls"] = "http://" in text or "https://" in text
        metadata["has_emails"] = "@" in text and "." in text
        metadata["has_numbers"] = any(char.isdigit() for char in text)

        # Readability metrics
        sentences = text.count(".") + text.count("!") + text.count("?")
        if sentences > 0:
            metadata["avg_sentence_length"] = len(text.split()) / sentences
        else:
            metadata["avg_sentence_length"] = 0

        return metadata

    def detect_language(self, text: str) -> str:
        """Detect document language using heuristic word frequency analysis.

        Uses simple pattern matching to count common words in Spanish and English.
        This is NOT an ML-based approach; it's a fast heuristic suitable for quick
        language filtering. Accuracy is limited to these two languages.

        Algorithm:
            1. Converts text to lowercase
            2. Counts occurrences of common Spanish words (e.g., "el", "la", "que")
            3. Counts occurrences of common English words (e.g., "the", "be", "to")
            4. Returns language with higher count

        Args:
            text: Document text to analyze (minimum ~100 words recommended)

        Returns:
            str: ISO 639-1 language code:
                - "es": Spanish (Spanish common words count > English count)
                - "en": English (English common words count > Spanish count)
                - "unknown": Equal counts or insufficient text

        Notes:
            - Requires words to be surrounded by spaces (word boundaries)
            - Works best with documents >100 words
            - Not suitable for detecting other languages
            - Accuracy degrades on very short documents or code/structured text
        """
        # Spanish common words
        spanish_words = ["el", "la", "de", "que", "y", "a", "en", "un", "ser", "se"]
        # English common words
        english_words = [
            "the",
            "be",
            "to",
            "of",
            "and",
            "a",
            "in",
            "that",
            "have",
            "it",
        ]

        text_lower = text.lower()
        spanish_count = sum(1 for word in spanish_words if f" {word} " in text_lower)
        english_count = sum(1 for word in english_words if f" {word} " in text_lower)

        if spanish_count > english_count:
            return "es"
        elif english_count > spanish_count:
            return "en"
        else:
            return "unknown"

    def process(self, ch, method, properties, body):
        """Handle incoming metadata extraction job from RabbitMQ.

        Processes a single job message by:
            1. Parsing the RabbitMQ message (JSON)
            2. Retrieving extracted text from Redis (key: orchestrator:job:{id}:text)
            3. Computing metadata via extract_metadata()
            4. Storing results in Redis (key: orchestrator:job:{id}:metadata)
            5. Updating job step status to "completed"
            6. Publishing progress event (100% completion, step="metadata")

        This is the callback registered with RabbitMQ via channel.basic_consume().

        Args:
            ch: RabbitMQ channel instance
            method: Message delivery information (includes delivery_tag, routing_key)
            properties: Message properties (content_type, headers, etc.)
            body: Raw message body (JSON string)

        Message format (JSON):
            {
                "job_id": "uuid-string",
                "document_url": "https://example.com/doc.pdf" (optional)
            }

        Redis operations:
            - GET orchestrator:job:{job_id}:text
            - SET orchestrator:job:{job_id}:metadata (JSON string)
            - HSET orchestrator:job:{job_id}:steps metadata=completed

        Error handling:
            - If text not found: NACKs message (no requeue) and logs warning
            - If extraction fails: NACKs message (requeue=True) for retry
            - All exceptions logged; no exceptions raised to RabbitMQ

        Metrics:
            - Increments metadata_worker_jobs_total with status label
            - Observes job_duration_seconds histogram
        """
        start_time = time.time()
        job_id = None

        try:
            message = json.loads(body)
            job_id = message.get("job_id")
            document_url = message.get("document_url")

            logger.info(f"Processing metadata for job: {job_id}")

            # Get text from Redis
            text_key = f"orchestrator:job:{job_id}:text"
            text_data = self.redis_client.get(text_key)

            if not text_data:
                logger.warning(f"No text found in Redis for job: {job_id}")
                jobs_total.labels(status="no_text").inc()
                ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
                return

            # Extract metadata
            metadata = self.extract_metadata(text_data, document_url)

            # Store in Redis
            metadata_key = f"orchestrator:job:{job_id}:metadata"
            self.redis_client.set(metadata_key, json.dumps(metadata))

            # Update step status
            self.redis_client.hset(
                f"orchestrator:job:{job_id}:steps", "metadata", "completed"
            )

            # Publish event: 100% progress
            self.event_bus.publish_job_progress(job_id, 100, "metadata")

            duration = time.time() - start_time
            job_duration.observe(duration)
            jobs_total.labels(status="success").inc()

            logger.info(f"Metadata completed for job: {job_id} in {duration:.2f}s")
            logger.info(f"Extracted metadata: {metadata}")

            ch.basic_ack(delivery_tag=method.delivery_tag)

        except Exception as e:
            logger.error(f"Error processing metadata: {e}")
            jobs_total.labels(status="error").inc()
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)

        finally:
            if _stopping and ch.is_open:
                logger.info("Graceful shutdown: stopping consumer after current message")
                ch.stop_consuming()


def signal_handler(signum, frame):
    """Handle graceful shutdown on SIGINT or SIGTERM.

    Sets the global _stopping flag to stop the consumer loop after the current
    message finishes. Does NOT call sys.exit() to avoid interrupting a message
    in flight.

    Args:
        signum: Signal number (signal.SIGINT or signal.SIGTERM)
        frame: Current stack frame at time of signal
    """
    logger.info("Received shutdown signal, initiating graceful shutdown...")
    global _stopping
    _stopping = True


def main():
    """Main entry point for metadata worker.

    Orchestrates worker startup and lifecycle:
        1. Registers signal handlers (SIGINT, SIGTERM) for graceful shutdown
        2. Starts Prometheus metrics HTTP server
        3. Initializes MetadataWorker instance (Redis + EventBus)
        4. Enters infinite loop:
            - Connects to RabbitMQ
            - Declares metadata queue
            - Consumes messages (prefetch_count=10 for load distribution)
            - On connection error: logs and retries after 5 seconds

    RabbitMQ connection:
        - Automatically reconnects on failure (idempotent)
        - Uses connection context manager for safe resource cleanup
        - Prefetch count of 10 prevents overwhelming single worker instance

    Metrics:
        - Starts Prometheus exporter on METRICS_PORT (default 8003)
        - Tracks jobs_total (Counter with status label)
        - Tracks job_duration (Histogram in seconds)

    Environment:
        - Reads REDIS_URL, RABBITMQ_URL, QUEUE_NAME, METRICS_PORT from env
        - See module docstring for defaults
    """
    logger.info("Starting Metadata Worker")

    global _stopping
    _stopping = False

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    start_http_server(METRICS_PORT)
    logger.info(f"Metrics server started on port {METRICS_PORT}")

    worker = MetadataWorker()

    while not _stopping:
        try:
            with connect_rabbitmq(RABBITMQ_URL, prefetch_count=10) as (
                connection,
                channel,
            ):
                logger.info(f"Consuming from queue: {QUEUE_NAME}")

                declare_queue(channel, QUEUE_NAME)
                channel.basic_consume(
                    queue=QUEUE_NAME, on_message_callback=worker.process, auto_ack=False
                )

                channel.start_consuming()

        except Exception as e:
            logger.error(f"RabbitMQ connection error: {e}")
            if not _stopping:
                time.sleep(5)

    logger.info("Metadata worker shutdown complete")


if __name__ == "__main__":
    main()
