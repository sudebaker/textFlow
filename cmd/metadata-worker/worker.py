#!/usr/bin/env python3
"""
Metadata extraction worker for textFlow.

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

import json
import logging
import sys
import hashlib
import mimetypes
from datetime import UTC, datetime
from typing import Dict, Optional

sys.path.insert(0, "/app")
from pkg.worker_common.artifact_store import STORE, resolve_text
from pkg.worker_common.base import BaseWorker

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


class MetadataWorker(BaseWorker):
    def __init__(self):
        super().__init__(
            worker_name="metadata-worker",
            queue_name="metadata",
            metrics_port=8003,
            requires_gpu=False,
        )

    def process_message(self, message: Dict) -> Dict:
        job_id = message.get("job_id")
        document_url = message.get("document_url")

        logger.info(f"Processing metadata for job: {job_id}")

        text_key = f"orchestrator:job:{job_id}:text"
        text_data = resolve_text(STORE, self.redis_client.get(text_key))

        if not text_data:
            raise ValueError(f"No text found in Redis for job: {job_id}")

        metadata = self._extract_metadata(text_data, document_url)

        metadata_key = f"orchestrator:job:{job_id}:metadata"
        self.redis_client.set(metadata_key, json.dumps(metadata))

        self.redis_client.hset(
            f"orchestrator:job:{job_id}:steps", "metadata", "completed"
        )

        self.event_bus.publish_job_progress(job_id, 100, "metadata")

        logger.info(f"Metadata completed for job: {job_id}")
        logger.info(f"Extracted metadata: {metadata}")

        return metadata

    def _extract_metadata(
        self, text: str, document_url: Optional[str] = None
    ) -> Dict:
        metadata = {
            "extracted_at": datetime.now(UTC).isoformat(),
            "text_length": len(text),
            "word_count": len(text.split()),
            "line_count": text.count("\n") + 1,
            "char_count": len(text),
        }

        metadata["content_hash"] = hashlib.sha256(text.encode("utf-8")).hexdigest()

        if document_url:
            metadata["source_url"] = document_url
            mime_type, _ = mimetypes.guess_type(document_url)
            if mime_type:
                metadata["mime_type"] = mime_type

        metadata["language"] = self._detect_language(text)

        metadata["has_urls"] = "http://" in text or "https://" in text
        metadata["has_emails"] = "@" in text and "." in text
        metadata["has_numbers"] = any(char.isdigit() for char in text)

        sentences = text.count(".") + text.count("!") + text.count("?")
        if sentences > 0:
            metadata["avg_sentence_length"] = len(text.split()) / sentences
        else:
            metadata["avg_sentence_length"] = 0

        return metadata

    def _detect_language(self, text: str) -> str:
        spanish_words = ["el", "la", "de", "que", "y", "a", "en", "un", "ser", "se"]
        english_words = [
            "the", "be", "to", "of", "and", "a", "in", "that", "have", "it",
        ]

        text_lower = text.lower()
        spanish_count = sum(1 for w in spanish_words if f" {w} " in text_lower)
        english_count = sum(1 for w in english_words if f" {w} " in text_lower)

        if spanish_count > english_count:
            return "es"
        elif english_count > spanish_count:
            return "en"
        return "unknown"


if __name__ == "__main__":
    worker = MetadataWorker()
    worker.run()
