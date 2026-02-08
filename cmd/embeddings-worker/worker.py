#!/usr/bin/env python3
"""
Embeddings Worker for IA Text Orchestrator
Consumes messages from RabbitMQ and generates embeddings for each chunk using BAAI/bge-m3
"""

import os
import json
import logging
import signal
import sys
import time
from contextlib import contextmanager
from typing import Dict, Optional, List, Any

import pika
import redis
import requests
from prometheus_client import Counter, Histogram, Gauge, start_http_server

from app.services.embeddings import EmbeddingService

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

sys.path.insert(0, "/app")
from pkg.events_python import EventBus

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

jobs_total = Counter("embeddings_worker_jobs_total", "Total jobs processed", ["status"])
job_duration = Histogram("embeddings_worker_job_duration_seconds", "Job duration")
gpu_available = Gauge("embeddings_worker_gpu_available", "GPU availability", ["device"])

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://rabbitmq:5672/")
RESOURCE_MANAGER_URL = os.getenv("RESOURCE_MANAGER_URL", "http://resource-manager:9090")
QUEUE_NAME = os.getenv("QUEUE_NAME", "embeddings")
METRICS_PORT = int(os.getenv("METRICS_PORT", "8001"))
MODEL_PATH = os.getenv("MODEL_PATH", "/models/bge-m3")
EMBEDDING_BATCH_SIZE = int(os.getenv("EMBEDDING_BATCH_SIZE", "16"))


class ResourceManagerClient:
    def __init__(self, base_url: str):
        self.base_url = base_url
        self._cache = None
        self._cache_time = 0
        self._cache_ttl = 60

    def get_resources(self) -> Dict:
        now = time.time()
        if self._cache and (now - self._cache_time) < self._cache_ttl:
            return self._cache

        try:
            resp = requests.get(f"{self.base_url}/api/v1/resources", timeout=5)
            resp.raise_for_status()
            self._cache = resp.json()
            self._cache_time = now
            return self._cache
        except Exception as e:
            logger.warning(f"Failed to get resources from manager: {e}")
            return {"gpu_available": False}


class EmbeddingsWorker:
    def __init__(self):
        self.redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        self.resource_manager = ResourceManagerClient(RESOURCE_MANAGER_URL)
        self.event_bus = EventBus(self.redis_client)
        self.service = None

    def get_resources(self) -> Dict:
        return self.resource_manager.get_resources()

    def load_model(self):
        resources = self.get_resources()
        use_gpu = resources.get("gpu_available", False)
        batch_size = EMBEDDING_BATCH_SIZE if use_gpu else 8

        gpu_available.labels(device="cuda:0").set(1 if use_gpu else 0)

        logger.info(
            f"Loading embeddings model from local path: {MODEL_PATH}, GPU: {use_gpu}, batch_size: {batch_size}"
        )
        self.service = EmbeddingService(model_path=MODEL_PATH)
        logger.info("Embeddings model loaded successfully from local cache")

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

            for i, chunk in enumerate(chunks):
                chunk_id = chunk.get("chunk_id")
                chunk_text = chunk.get("text", "")

                if not chunk_text:
                    logger.warning(f"Empty text for chunk {chunk_id}")
                    continue

                embedding = self.service.generate_embeddings(chunk_text)
                embeddings_dict[chunk_id] = embedding

                if (i + 1) % 10 == 0 or (i + 1) == total_chunks:
                    logger.info(
                        f"Generated embeddings for chunk {i + 1}/{total_chunks}"
                    )

            embeddings_key = f"orchestrator:job:{job_id}:embeddings"
            self.redis_client.set(embeddings_key, json.dumps(embeddings_dict))

            self.redis_client.hset(
                f"orchestrator:job:{job_id}:steps", "embeddings", "completed"
            )

            self.event_bus.publish_job_progress(job_id, 33, "embedding")

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
            if job_id:
                self.redis_client.hset(
                    f"orchestrator:job:{job_id}:status", mapping={"embeddings": "error"}
                )
                self.event_bus.publish_job_failed(job_id, str(e))
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)


def parse_rabbitmq_url(url: str) -> pika.ConnectionParameters:
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
    for attempt in range(max_retries):
        try:
            params = parse_rabbitmq_url(url)
            connection = pika.BlockingConnection(params)
            channel = connection.channel()
            prefetch_count = int(os.getenv("PREFETCH_COUNT", "5"))
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
    logger.info("Starting Embeddings Worker")

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    start_http_server(METRICS_PORT)
    logger.info(f"Metrics server started on port {METRICS_PORT}")

    worker = EmbeddingsWorker()
    worker.load_model()

    while True:
        try:
            with connect_rabbitmq(RABBITMQ_URL) as (connection, channel):
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
