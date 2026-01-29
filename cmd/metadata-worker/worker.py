#!/usr/bin/env python3
"""
Metadata Worker for IA Text Orchestrator
Consumes messages from RabbitMQ and extracts document metadata
"""

import os
import json
import logging
import signal
import sys
import time
from contextlib import contextmanager
from typing import Dict, Optional
from datetime import datetime
import hashlib
import mimetypes

import pika
import redis
import requests
from prometheus_client import Counter, Histogram, start_http_server

# Import event bus
# In Docker: worker.py is at /app/worker.py, pkg is at /app/pkg
sys.path.insert(0, "/app")
from pkg.events_python import EventBus

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

jobs_total = Counter("metadata_worker_jobs_total", "Total jobs processed", ["status"])
job_duration = Histogram("metadata_worker_job_duration_seconds", "Job duration")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://localhost:5672/")
QUEUE_NAME = os.getenv("QUEUE_NAME", "metadata")
METRICS_PORT = int(os.getenv("METRICS_PORT", "8003"))


class MetadataWorker:
    def __init__(self):
        self.redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        self.event_bus = EventBus(self.redis_client)

    def extract_metadata(self, text: str, document_url: Optional[str] = None) -> Dict:
        """Extract comprehensive metadata from document"""
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
        """Simple language detection based on common words"""
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
            if job_id:
                self.redis_client.hset(
                    f"job:{job_id}:status", mapping={"metadata": "error"}
                )
                # Publish failed event
                self.event_bus.publish_job_failed(job_id, str(e))
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)


def parse_rabbitmq_url(url: str) -> pika.ConnectionParameters:
    """Parse AMQP URL and return ConnectionParameters.

    Supports URLs like: amqp://user:pass@host:port/vhost
    """
    from urllib.parse import urlparse

    parsed = urlparse(url)

    credentials = pika.PlainCredentials(
        parsed.username or "guest", parsed.password or "guest"
    )

    return pika.ConnectionParameters(
        host=parsed.hostname or "localhost",
        port=parsed.port or 5672,
        virtual_host=parsed.path[1:] if parsed.path else "/",
        credentials=credentials,
        heartbeat=600,
        blocked_connection_timeout=300,
    )


@contextmanager
def connect_rabbitmq(url: str, max_retries: int = 5):
    """Connect to RabbitMQ with retry logic."""
    for attempt in range(max_retries):
        try:
            params = parse_rabbitmq_url(url)
            connection = pika.BlockingConnection(params)
            channel = connection.channel()
            prefetch_count = int(os.getenv("PREFETCH_COUNT", "10"))
            channel.basic_qos(prefetch_count=prefetch_count)
            logger.info(
                f"Connected to RabbitMQ at {params.host}:{params.port} with prefetch_count={prefetch_count}"
            )
            yield connection, channel
            return
        except Exception as e:
            logger.warning(
                f"Failed to connect to RabbitMQ (attempt {attempt + 1}/{max_retries}): {e}"
            )
            if attempt < max_retries - 1:
                time.sleep(2**attempt)
    raise Exception("Failed to connect to RabbitMQ after max retries")


def signal_handler(signum, frame):
    logger.info("Received shutdown signal, stopping worker...")
    sys.exit(0)


def main():
    logger.info("Starting Metadata Worker")

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    start_http_server(METRICS_PORT)
    logger.info(f"Metrics server started on port {METRICS_PORT}")

    worker = MetadataWorker()

    while True:
        try:
            with connect_rabbitmq(RABBITMQ_URL) as (connection, channel):
                channel.queue_declare(queue=QUEUE_NAME, durable=True)
                logger.info(f"Consuming from queue: {QUEUE_NAME}")

                channel.basic_consume(
                    queue=QUEUE_NAME, on_message_callback=worker.process, auto_ack=False
                )

                channel.start_consuming()

        except Exception as e:
            logger.error(f"RabbitMQ connection error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
