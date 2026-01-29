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
from typing import Dict, List, Optional

import pika
import redis
import requests
from prometheus_client import Counter, Histogram, Gauge, start_http_server

from app.config.settings import Settings

# Add parent directory to path for pkg imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
from pkg.events_python import EventBus

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

settings = Settings()

jobs_total = Counter("entities_worker_jobs_total", "Total jobs processed", ["status"])
job_duration = Histogram("entities_worker_job_duration_seconds", "Job duration")
gpu_available = Gauge("entities_worker_gpu_available", "GPU availability", ["device"])

REDIS_URL = os.getenv("REDIS_URL", settings.redis_url)
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/")
QUEUE_NAME = os.getenv("QUEUE_NAME", settings.entities_queue)
METRICS_PORT = int(os.getenv("METRICS_PORT", "8002"))


class EntitiesWorker:
    def __init__(self):
        self.redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        self.event_bus = EventBus(self.redis_client)
        self.model = None
        self.device = "cpu"
        self.default_entities = ["PER", "ORG", "LOC", "DATE", "MONEY"]

    def load_model(self):
        from gliner import GLiNER

        model_path = os.getenv("GLINER_MODEL_PATH", "/models/gliner_model")
        logger.info(f"Loading GLiNER from: {model_path}")
        self.model = GLiNER.from_pretrained(model_path)
        logger.info("GLiNER model loaded successfully")

    def process(self, ch, method, properties, body):
        start_time = time.time()
        job_id = None

        try:
            message = json.loads(body)
            job_id = message.get("job_id")
            logger.info(f"Processing entities for job: {job_id}")

            text_key = f"job:{job_id}:text"
            text_data = self.redis_client.get(text_key)

            if not text_data:
                logger.warning(f"No text found in Redis for job: {job_id}")
                jobs_total.labels(status="no_text").inc()
                ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
                return

            entities = self.model.predict_entities(
                [text_data], self.default_entities, threshold=0.8
            )

            entities_list = [
                {
                    "text": e["text"],
                    "label": e["label"],
                    "confidence": e["score"],
                    "start": e.get("start", 0),
                    "end": e.get("end", 0),
                }
                for e in entities[0]
            ]

            entities_key = f"job:{job_id}:entities"
            self.redis_client.set(entities_key, json.dumps(entities_list))

            self.redis_client.hset(
                f"job:{job_id}:status", mapping={"entities": "completed"}
            )

            # Publish progress event (entities extraction = 66% complete)
            self.event_bus.publish_job_progress(job_id, 66, "entities")

            duration = time.time() - start_time
            job_duration.observe(duration)
            jobs_total.labels(status="success").inc()

            logger.info(
                f"Entities completed for job: {job_id} in {duration:.2f}s, found {len(entities_list)} entities"
            )

            ch.basic_ack(delivery_tag=method.delivery_tag)

        except Exception as e:
            logger.error(f"Error processing entities: {e}")
            jobs_total.labels(status="error").inc()
            if job_id:
                self.redis_client.hset(
                    f"job:{job_id}:status", mapping={"entities": "error"}
                )
                # Publish failure event
                self.event_bus.publish_job_failed(job_id, str(e))
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)


def parse_rabbitmq_url(url: str) -> pika.ConnectionParameters:
    """Parse AMQP URL and return ConnectionParameters.

    Supports URLs like: amqp://user:pass@host:port/vhost
    """
    from urllib.parse import urlparse

    parsed = urlparse(url)

    credentials = pika.PlainCredentials(
        parsed.username or 'guest',
        parsed.password or 'guest'
    )

    return pika.ConnectionParameters(
        host=parsed.hostname or 'localhost',
        port=parsed.port or 5672,
        virtual_host=parsed.path[1:] if parsed.path else '/',
        credentials=credentials,
        heartbeat=600,
        blocked_connection_timeout=300
    )


@contextmanager
def connect_rabbitmq(url: str, max_retries: int = 5):
    """Connect to RabbitMQ with retry logic."""
    for attempt in range(max_retries):
        try:
            params = parse_rabbitmq_url(url)
            connection = pika.BlockingConnection(params)
            channel = connection.channel()
            channel.basic_qos(prefetch_count=1)
            logger.info(f"Connected to RabbitMQ at {params.host}:{params.port}")
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
