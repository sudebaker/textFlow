#!/usr/bin/env python3
"""
Embeddings Worker for textFlow
Consumes messages from RabbitMQ and generates embeddings for each chunk using BAAI/bge-m3

⭐ GPU Features:
- Automatic GPU detection via torch.cuda
- Adaptive batching (GPU=32, CPU=2)
- FP16 optimization for GPU (in EmbeddingService)
"""

import os
import sys

# CRITICAL: Set offline mode BEFORE importing any HuggingFace or transformers packages
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

import json
import logging
import signal
import time
from typing import Dict, Optional, List, Any

import msgpack
import pika
import redis
import requests
import torch
from prometheus_client import Counter, Histogram, Gauge, start_http_server

sys.path.insert(0, "/app")
from pkg.events_python import EventBus
from pkg.worker_common.rabbitmq import (
    parse_rabbitmq_url,
    connect_rabbitmq,
    declare_queue,
)
from app.services.embeddings import EmbeddingService

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

_stopping = False

jobs_total = Counter("embeddings_worker_jobs_total", "Total jobs processed", ["status"])
job_duration = Histogram("embeddings_worker_job_duration_seconds", "Job duration")
gpu_available = Gauge("embeddings_worker_gpu_available", "GPU availability", ["device"])
gpu_memory_gb = Gauge("embeddings_worker_gpu_memory_gb", "GPU memory usage in GB")

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://rabbitmq:5672/")
QUEUE_NAME = os.getenv("QUEUE_NAME", "embeddings")
METRICS_PORT = int(os.getenv("METRICS_PORT", "8001"))
MODEL_PATH = os.getenv("MODEL_PATH", "/models/bge-m3")
EMBEDDING_BATCH_SIZE_GPU = int(os.getenv("EMBEDDING_BATCH_SIZE_GPU", "32"))
EMBEDDING_BATCH_SIZE_CPU = int(os.getenv("EMBEDDING_BATCH_SIZE_CPU", "2"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))

# GPU/CPU device selection - normalize empty string to None for auto-detection
_device_env = os.getenv("EMBEDDINGS_DEVICE", "").strip()
EMBEDDINGS_DEVICE = _device_env if _device_env else None


def detect_gpu() -> bool:
    """Check if GPU is available."""
    if torch is None:
        return False
    return torch.cuda.is_available()


# NOTE: _get_retry_count and _should_retry are duplicated from pkg/worker_common/base.py
# Refactor if these workers migrate to BaseWorker.
def _get_retry_count(properties) -> int:
    """Extract retry count from RabbitMQ x-death headers."""
    if properties.headers and "x-death" in properties.headers:
        return sum(d.get("count", 0) for d in properties.headers["x-death"])
    return 0


def _should_retry(properties) -> bool:
    """Return True if message should be requeued (under MAX_RETRIES)."""
    return _get_retry_count(properties) < MAX_RETRIES


class EmbeddingsWorker:
    def __init__(self):
        self.redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        self.event_bus = EventBus(self.redis_client)
        self.service = None
        self.batch_size = EMBEDDING_BATCH_SIZE_CPU

    def _check_micro_inferences_exist(self, job_id: str) -> bool:
        """Check if micro_inferences exist in Redis for this job."""
        key = f"orchestrator:job:{job_id}:micro_inferences"
        return self.redis_client.exists(key) > 0

    def _load_micro_inferences(self, job_id: str) -> List[Dict[str, Any]]:
        """Load and parse micro_inferences from Redis."""
        key = f"orchestrator:job:{job_id}:micro_inferences"
        raw = self.redis_client.get(key)
        if not raw:
            return []
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.warning(f"Failed to parse micro_inferences for job: {job_id}")
            return []

    def _generate_inference_embeddings(
        self, micro_inferences: List[Dict[str, Any]]
    ) -> Dict[str, Dict[str, List[float]]]:
        """Generate embeddings for each micro-inference text.
        
        Args:
            micro_inferences: List of dicts with chunk_id and inferences
            
        Returns:
            Dict mapping chunk_id -> {inference_idx: embedding_vector}
        """
        if not micro_inferences:
            return {}

        inference_embeddings: Dict[str, Dict[str, List[float]]] = {}

        for chunk_data in micro_inferences:
            chunk_id = chunk_data.get("chunk_id")
            inferences = chunk_data.get("inferences", [])

            if not chunk_id or not inferences:
                continue

            inference_texts = [inf.get("text") or "" for inf in inferences]

            if not any(inference_texts):
                continue

            embeddings = self.service.generate_embeddings(
                inference_texts, batch_size=self.batch_size
            )

            chunk_embeddings: Dict[str, List[float]] = {}
            for idx, embedding in enumerate(embeddings):
                chunk_embeddings[f"inference_{idx}"] = embedding

            inference_embeddings[chunk_id] = chunk_embeddings

        return inference_embeddings

    def _save_inference_embeddings(
        self, job_id: str, inference_embeddings: Dict[str, Any]
    ) -> None:
        """Save inference embeddings to Redis using MessagePack."""
        if not inference_embeddings:
            return
        key = f"orchestrator:job:{job_id}:inference_embeddings"
        self.redis_client.set(
            key,
            msgpack.packb(inference_embeddings, use_bin_type=True)
        )
        self.redis_client.expire(key, 86400)

    def load_model(self):
        use_gpu = detect_gpu()
        self.batch_size = (
            EMBEDDING_BATCH_SIZE_GPU if use_gpu else EMBEDDING_BATCH_SIZE_CPU
        )

        # Update metrics
        gpu_available.labels(device="cuda:0").set(1 if use_gpu else 0)

        if use_gpu:
            gpu_name = (
                torch.cuda.get_device_name() if torch.cuda.is_available() else "Unknown"
            )
            logger.info(f"🚀 GPU Mode detected: {gpu_name}")
            logger.info(f"   Batch size: {self.batch_size} (optimized for GPU)")
        else:
            logger.info(f"📝 CPU Mode detected")
            logger.info(f"   Batch size: {self.batch_size} (conservative for CPU)")

        logger.info(f"Loading embeddings model from: {MODEL_PATH}")
        self.service = EmbeddingService(model_path=MODEL_PATH, device=EMBEDDINGS_DEVICE)
        logger.info("✅ Embeddings model loaded successfully")

    def process(self, ch, method, properties, body):
        start_time = time.time()
        job_id = None

        try:
            message = json.loads(body)
            job_id = message.get("job_id")
            chunks = message.get("chunks", [])

            logger.info(
                f"Processing embeddings for job: {job_id} with {len(chunks)} chunks"
            )

            if not chunks:
                chunks_json = self.redis_client.get(f"orchestrator:job:{job_id}:chunks")
                if chunks_json:
                    chunks = json.loads(chunks_json)
                else:
                    logger.warning(
                        f"No chunks found in message or Redis for job: {job_id}"
                    )
                    jobs_total.labels(status="no_chunks").inc()
                    ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
                    return

            embeddings_dict = {}
            total_chunks = len(chunks)

            # ⭐ Adaptive batching: process chunks in batches for efficiency
            logger.info(
                f"Processing {total_chunks} chunks with batch_size={self.batch_size}"
            )

            chunk_texts = [chunk.get("text", "") for chunk in chunks]
            chunk_ids = [chunk.get("chunk_id") for chunk in chunks]

            # Process in batches
            for batch_start in range(0, len(chunk_texts), self.batch_size):
                batch_end = min(batch_start + self.batch_size, len(chunk_texts))
                batch_texts = chunk_texts[batch_start:batch_end]
                batch_ids = chunk_ids[batch_start:batch_end]

                # Generate embeddings for batch
                batch_embeddings = self.service.generate_embeddings(
                    batch_texts, batch_size=self.batch_size
                )

                # Store results
                for chunk_id, embedding in zip(batch_ids, batch_embeddings):
                    if chunk_id:
                        embeddings_dict[chunk_id] = embedding

                processed = min(batch_end, total_chunks)
                logger.info(
                    f"Generated embeddings for chunk {processed}/{total_chunks}"
                )

            # Update GPU memory metrics
            if detect_gpu():
                gpu_memory_gb.set(torch.cuda.memory_allocated() / 1024**3)

            embeddings_key = f"orchestrator:job:{job_id}:embeddings"
            # Use MessagePack binary serialization (~75% smaller than JSON for float32 vectors)
            self.redis_client.set(
                embeddings_key,
                msgpack.packb(embeddings_dict, use_bin_type=True)
            )

            self.redis_client.hset(
                f"orchestrator:job:{job_id}:steps", "embeddings", "completed"
            )

            inference_progress = 33
            if self._check_micro_inferences_exist(job_id):
                logger.info(f"Generating inference embeddings for job: {job_id}")
                try:
                    micro_inferences = self._load_micro_inferences(job_id)
                    if micro_inferences:
                        inference_embeddings = self._generate_inference_embeddings(micro_inferences)
                        self._save_inference_embeddings(job_id, inference_embeddings)
                        self.redis_client.hset(
                            f"orchestrator:job:{job_id}:steps", "inference_embeddings", "completed"
                        )
                        inference_count = sum(
                            len(c.get("inferences", [])) 
                            for c in micro_inferences
                        )
                        logger.info(
                            f"Generated inference embeddings for {inference_count} inferences"
                        )
                        inference_progress = 40
                except Exception as e:
                    logger.warning(f"Failed to generate inference embeddings: {e}")

            self.event_bus.publish_job_progress(job_id, inference_progress, "embedding")

            duration = time.time() - start_time
            job_duration.observe(duration)
            jobs_total.labels(status="success").inc()

            logger.info(
                f"Embeddings completed for job: {job_id} in {duration:.2f}s ({total_chunks} chunks)"
            )

            ch.basic_ack(delivery_tag=method.delivery_tag)

        except Exception as e:
            logger.error(f"Error processing embeddings: {e}")
            jobs_total.labels(status="error").inc()
            requeue = _should_retry(properties)
            if not requeue:
                logger.warning(
                    f"Message exceeded max retries ({MAX_RETRIES}), sending to DLQ"
                )
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=requeue)

        finally:
            if _stopping and ch.is_open:
                logger.info("Graceful shutdown: stopping consumer after current message")
                ch.stop_consuming()


def signal_handler(signum, frame):
    logger.info("Received shutdown signal, initiating graceful shutdown...")
    global _stopping
    _stopping = True


def main():
    logger.info("Starting Embeddings Worker")

    global _stopping
    _stopping = False

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    start_http_server(METRICS_PORT)
    logger.info(f"Metrics server started on port {METRICS_PORT}")

    worker = EmbeddingsWorker()
    worker.load_model()

    while not _stopping:
        try:
            with connect_rabbitmq(RABBITMQ_URL) as (connection, channel):
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

    logger.info("Embeddings worker shutdown complete")


if __name__ == "__main__":
    main()
