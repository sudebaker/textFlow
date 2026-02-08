#!/usr/bin/env python3
"""
Embeddings Worker for IA Text Orchestrator (Refactored using BaseWorker)

This is an example of how to refactor the existing embeddings-worker to use
the new BaseWorker class from pkg/worker_common.base.

Benefits:
- ~60% less code (from ~227 lines to ~90 lines)
- Standardized logging, metrics, health checks
- Graceful shutdown handling
- RabbitMQ connection with retry logic built-in

Compare with worker.py to see the improvements.
"""

import os
import sys
import json
import logging

sys.path.insert(0, "/app")

from pkg.worker_common.base import BaseWorker
from app.services.embeddings import EmbeddingService


class EmbeddingsWorker(BaseWorker):
    """
    Embeddings worker that uses BaseWorker for common functionality.

    This worker processes text documents and generates embeddings
    using the BAAI/bge-m3 model.
    """

    def __init__(self):
        """
        Initialize EmbeddingsWorker.

        Note: All the Redis, RabbitMQ, metrics setup is handled by BaseWorker.
        """
        super().__init__(
            worker_name="embeddings-worker",
            queue_name=os.getenv("QUEUE_NAME", "embeddings"),
            metrics_port=int(os.getenv("METRICS_PORT", "8001")),
            requires_gpu=True,
        )

        self.service = None

    def load_model(self):
        """Load the embeddings model."""
        resources = self.get_resources()
        use_gpu = resources.get("gpu_available", False)
        batch_size = 64 if use_gpu else 16

        self.gpu_available.labels(device="cuda:0").set(1 if use_gpu else 0)

        self.logger.info(
            f"Loading embeddings model on GPU: {use_gpu}, batch_size: {batch_size}"
        )
        self.service = EmbeddingService()
        self.logger.info("Embeddings model loaded successfully")

    def process_message(self, message: dict) -> dict:
        """
        Process a single message from the queue.

        This method is called automatically by BaseWorker._on_message().

        Args:
            message: Dictionary containing job_id and optional document data

        Returns:
            Processing result dictionary
        """
        job_id = message.get("job_id")
        self.job_logger.start_job(job_id)

        try:
            # Get text from Redis
            text_key = f"orchestrator:job:{job_id}:text"
            text_data = self.redis_client.get(text_key)

            if not text_data:
                self.job_logger.log_warning("No text found in Redis")
                return {"status": "no_text", "job_id": job_id}

            # Generate embeddings
            embeddings = self.service.generate_embeddings(text_data)

            # Store embeddings in Redis
            embeddings_key = f"orchestrator:job:{job_id}:embeddings"
            self.redis_client.set(embeddings_key, json.dumps(embeddings))

            # Update step status
            self.redis_client.hset(
                f"orchestrator:job:{job_id}:steps", "embeddings", "completed"
            )

            # Publish progress event
            self.event_bus.publish_job_progress(job_id, 33, "embedding")

            self.job_logger.log_processing("Embeddings generated successfully")
            return {
                "status": "success",
                "job_id": job_id,
                "embeddings_size": len(embeddings),
            }

        except Exception as e:
            self.job_logger.log_error("Processing failed", error=e)
            # Update job status
            self.redis_client.hset(
                f"orchestrator:job:{job_id}:status", mapping={"embeddings": "error"}
            )
            self.event_bus.publish_job_failed(job_id, str(e))
            raise


def main():
    """Main entry point."""
    worker = EmbeddingsWorker()
    worker.load_model()
    worker.run()


if __name__ == "__main__":
    main()


# =============================================================================
# COMPARISON: worker.py vs worker_refactored.py
# =============================================================================
#
# BEFORE (worker.py - 227 lines):
# - Manual logging setup with basicConfig
# - Manual Redis connection
# - Manual RabbitMQ connection with retry logic (50+ lines)
# - Manual ResourceManagerClient class
# - Manual signal handlers
# - Manual metrics setup
# - No health checks
#
# AFTER (worker_refactored.py - ~90 lines):
# - All common functionality in BaseWorker
# - Standardized logging with JobLogger
# - Built-in health endpoints (/health, /metrics, /ready)
# - Graceful shutdown handling
# - Prometheus metrics built-in
# - RabbitMQ retry logic built-in
#
# CODE REDUCTION: ~60%
# BETTER MAINTAINABILITY: Yes
# CONSISTENT BEHAVIOR: Yes
#
# =============================================================================
