#!/usr/bin/env python3
"""
Entities Worker for IA Text Orchestrator
Consumes messages from RabbitMQ and extracts entities using GLiNER
"""

import os
import json
import logging
import signal
import sys
import time
from contextlib import contextmanager
from typing import Dict, List, Optional, Any

import pika
import redis
import requests
from prometheus_client import Counter, Histogram, Gauge, start_http_server

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

sys.path.insert(0, "/app")
from pkg.events_python import EventBus

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

jobs_total = Counter("entities_worker_jobs_total", "Total jobs processed", ["status"])
job_duration = Histogram("entities_worker_job_duration_seconds", "Job duration")
gpu_available = Gauge("entities_worker_gpu_available", "GPU availability", ["device"])

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://rabbitmq:5672/")
QUEUE_NAME = os.getenv("QUEUE_NAME", "entities")
METRICS_PORT = int(os.getenv("METRICS_PORT", "8002"))
GLINER_MODEL_PATH = os.getenv("GLINER_MODEL_PATH", "/models/gliner-small")
ENTITY_TYPES = os.getenv("ENTITY_TYPES", "PER,ORG,LOC,DATE,MONEY")
ENTITY_THRESHOLD = float(os.getenv("ENTITY_THRESHOLD", "0.8"))


class EntitiesWorker:
    def __init__(self):
        self.redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        self.event_bus = EventBus(self.redis_client)
        self.model = None
        self.device = "cpu"
        self.default_entities = [e.strip() for e in ENTITY_TYPES.split(",")]

    def load_model(self):
        from gliner import GLiNER
        from pathlib import Path

        model_path = Path(GLINER_MODEL_PATH)
        config_file = model_path / "gliner_config.json"

        if config_file.exists():
            logger.info(f"Loading GLiNER from local path: {GLINER_MODEL_PATH}")
            self.model = GLiNER.from_pretrained(str(model_path))
            logger.info("GLiNER model loaded successfully from local cache")
        else:
            logger.info(
                f"Local model not found at {GLINER_MODEL_PATH}, downloading from HuggingFace..."
            )
            self.model = GLiNER.from_pretrained("urchade/gliner_small")
            logger.info("GLiNER model loaded from HuggingFace")

    def process(self, ch, method, properties, body):
        start_time = time.time()
        job_id = None

        try:
            message = json.loads(body)
            job_id = message.get("job_id")
            chunks = message.get("chunks", [])

            entity_types = message.get("entity_types", self.default_entities)

            logger.info(
                f"Processing entities for job: {job_id} with {len(chunks)} chunks"
            )
            logger.info(f"Entity types: {entity_types}")

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

            all_entities = []
            entity_id = 0

            for chunk in chunks:
                chunk_id = chunk.get("chunk_id")
                chunk_text = chunk.get("text", "")

                if not chunk_text:
                    continue

                try:
                    entities = self.model.predict_entities(
                        chunk_text, entity_types, threshold=ENTITY_THRESHOLD
                    )

                    if entities and len(entities) > 0 and isinstance(entities[0], list):
                        entities_items = entities[0]
                    elif entities and isinstance(entities, list):
                        entities_items = entities
                    else:
                        entities_items = []

                    for e in entities_items:
                        all_entities.append(
                            {
                                "entity_id": f"ent_{entity_id:03d}",
                                "text": e.get("text", ""),
                                "label": e.get("label", ""),
                                "confidence": e.get("score", 0.0),
                                "chunk_id": chunk_id,
                                "position_in_chunk": e.get("start", 0),
                            }
                        )
                        entity_id += 1

                except Exception as e:
                    logger.warning(
                        f"Error extracting entities from chunk {chunk_id}: {e}"
                    )
                    continue

            entities_key = f"orchestrator:job:{job_id}:entities"
            self.redis_client.set(entities_key, json.dumps(all_entities))

            self.redis_client.hset(
                f"orchestrator:job:{job_id}:steps", "entities", "completed"
            )

            self.event_bus.publish_job_progress(job_id, 66, "entities")

            duration = time.time() - start_time
            job_duration.observe(duration)
            jobs_total.labels(status="success").inc()

            logger.info(
                f"Entities completed for job: {job_id} in {duration:.2f}s, found {len(all_entities)} entities"
            )

            ch.basic_ack(delivery_tag=method.delivery_tag)

        except Exception as e:
            logger.error(f"Error processing entities: {e}")
            jobs_total.labels(status="error").inc()
            if job_id:
                self.redis_client.hset(
                    f"orchestrator:job:{job_id}:status", mapping={"entities": "error"}
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
    logger.info("Starting Entities Worker")

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    start_http_server(METRICS_PORT)
    logger.info(f"Metrics server started on port {METRICS_PORT}")

    worker = EntitiesWorker()
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
