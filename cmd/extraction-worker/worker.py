#!/usr/bin/env python3
import os
import sys
import json
import base64
import logging
import pika
import redis
import requests
from typing import Dict, Optional
from urllib.parse import urlparse

# Configuración
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://localhost:5672/")
UNSTRUCTURED_URL = os.getenv("UNSTRUCTURED_URL", "http://unstructured:8000")
QUEUE_NAME = os.getenv("QUEUE_NAME", "extract_text")
PREFETCH_COUNT = int(os.getenv("PREFETCH_COUNT", "3"))

# Agregar ruta para eventos
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
from pkg.events_python import EventBus

logger = logging.getLogger(__name__)

def parse_rabbitmq_url(url: str) -> pika.ConnectionParameters:
    """Parse AMQP URL"""
    parsed = urlparse(url)
    credentials = pika.PlainCredentials(
        parsed.username or "guest",
        parsed.password or "guest"
    )
    return pika.ConnectionParameters(
        host=parsed.hostname or "localhost",
        port=parsed.port or 5672,
        virtual_host=parsed.path[1:] if parsed.path else "/",
        credentials=credentials,
        heartbeat=600,
        blocked_connection_timeout=300,
    )

class ExtractionWorker:
    def __init__(self):
        self.redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        self.event_bus = EventBus(self.redis_client)

    def extract_text_from_base64(self, document_base64: str) -> str:
        """Extract text from base64-encoded document"""
        try:
            # Decode base64
            document_bytes = base64.b64decode(document_base64)

            # Send to Unstructured API
            response = requests.post(
                f"{UNSTRUCTURED_URL}/general/v0/general",
                files={"files": ("document", document_bytes)},
                timeout=60
            )
            response.raise_for_status()

            # Parse response and extract text
            elements = response.json()
            text = "\n".join([elem.get("text", "") for elem in elements])
            return text

        except Exception as e:
            logger.error(f"Failed to extract text from base64: {e}")
            raise

    def extract_text_from_url(self, document_url: str) -> str:
        """Extract text from document URL"""
        try:
            # Download document
            response = requests.get(document_url, timeout=30)
            response.raise_for_status()
            document_bytes = response.content

            # Send to Unstructured API
            response = requests.post(
                f"{UNSTRUCTURED_URL}/general/v0/general",
                files={"files": ("document", document_bytes)},
                timeout=60
            )
            response.raise_for_status()

            # Parse response and extract text
            elements = response.json()
            text = "\n".join([elem.get("text", "") for elem in elements])
            return text

        except Exception as e:
            logger.error(f"Failed to extract text from URL: {e}")
            raise

    def process_message(self, ch, method, properties, body):
        """Process extraction job message"""
        job_id = None
        try:
            # Parse message
            message = json.loads(body)
            job_id = message.get("job_id")

            logger.info(f"Processing text extraction for job: {job_id}")

            # Update status to extracting
            self.redis_client.hset(f"orchestrator:job:{job_id}:status", "status", "extracting")

            # Extract text
            if message.get("document_base64"):
                text = self.extract_text_from_base64(message["document_base64"])
            elif message.get("document_url"):
                text = self.extract_text_from_url(message["document_url"])
            else:
                raise ValueError("No document provided")

            # Store text in Redis
            self.redis_client.set(f"orchestrator:job:{job_id}:text", text)
            logger.info(f"Stored text for job {job_id}: {len(text)} characters")

            # Update step status
            self.redis_client.hset(f"orchestrator:job:{job_id}:steps", "extraction", "completed")

            # Update status to processing
            self.redis_client.hset(f"orchestrator:job:{job_id}:status", "status", "processing")

            # Publish to parallel processing queues
            params = parse_rabbitmq_url(RABBITMQ_URL)
            connection = pika.BlockingConnection(params)
            channel = connection.channel()

            # Include document_url if available (for metadata worker)
            job_message = {"job_id": job_id}
            if message.get("document_url"):
                job_message["document_url"] = message["document_url"]

            job_message_json = json.dumps(job_message)
            for queue in ["embeddings", "entities", "metadata"]:
                channel.basic_publish(
                    exchange="",
                    routing_key=queue,
                    body=job_message_json,
                    properties=pika.BasicProperties(
                        delivery_mode=2,  # Persistent
                        content_type="application/json"
                    )
                )
                logger.info(f"Published job {job_id} to queue: {queue}")

            connection.close()

            # Publish progress event (text extraction complete)
            self.event_bus.publish_job_progress(job_id, 25, "processing")

            # Acknowledge message
            ch.basic_ack(delivery_tag=method.delivery_tag)
            logger.info(f"Text extraction completed for job: {job_id}")

        except Exception as e:
            logger.error(f"Error processing extraction: {e}")
            if job_id:
                self.redis_client.hset(f"orchestrator:job:{job_id}:status", "status", "failed")
                self.redis_client.set(f"orchestrator:job:{job_id}:error", str(e))
                self.event_bus.publish_job_failed(job_id, str(e))
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

def main():
    worker = ExtractionWorker()

    params = parse_rabbitmq_url(RABBITMQ_URL)
    connection = pika.BlockingConnection(params)
    channel = connection.channel()

    channel.basic_qos(prefetch_count=PREFETCH_COUNT)
    channel.basic_consume(
        queue=QUEUE_NAME,
        on_message_callback=worker.process_message,
        auto_ack=False
    )

    logger.info(f"Extraction worker started. Consuming from queue: {QUEUE_NAME}")
    channel.start_consuming()

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    main()
