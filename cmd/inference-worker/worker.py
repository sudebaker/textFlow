#!/usr/bin/env python3
"""
Inference Worker for IA Text Orchestrator
Consumes messages from RabbitMQ and extracts micro-inferences using an LLM
"""

import os
import sys
import json
import logging
import time
import re
import redis
import pika
import requests
from typing import Dict, List, Any, Optional
from prometheus_client import Counter, Histogram, start_http_server

sys.path.insert(0, "/app")
from pkg.worker_common.rabbitmq import parse_rabbitmq_url, connect_rabbitmq, declare_queue
from pkg.events_python import EventBus

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://rabbitmq:5672/")
QUEUE_NAME = os.getenv("QUEUE_NAME", "inferences")
METRICS_PORT = int(os.getenv("METRICS_PORT", "8006"))
LLM_URL = os.getenv("LLM_URL", "")  # Base URL without /v1 path
LLM_MODEL = os.getenv("LLM_MODEL", "qwen3-coder")  # vLLM model name

# Prometheus metrics
jobs_total = Counter("inference_worker_jobs_total", "Total jobs processed", ["status"])
job_duration = Histogram("inference_worker_job_duration_seconds", "Job duration")


class InferenceWorker:
    def __init__(self):
        self.redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        self.event_bus = EventBus(self.redis_client)

    def extract_inferences(
        self, text: str, max_inferences: int = 5
    ) -> List[Dict[str, Any]]:
        """
        Extract micro-inferences from text using an LLM.
        
        Args:
            text: Document text to extract inferences from
            max_inferences: Maximum number of inferences to extract
            
        Returns:
            List of {"fact": str, "confidence": float, "source": "llm"}
        """
        if not LLM_URL:
            logger.warning("No LLM_URL configured, skipping inferences")
            return []

        try:
            # Truncate text to first 2000 chars for LLM context
            truncated_text = text[:2000]
            
            prompt = f"""Extract up to {max_inferences} key facts from the following document text.
Return ONLY a JSON array of objects with "fact" and "confidence" (0.0-1.0) keys.
Example: [{{"fact": "The property value is 500,000 EUR", "confidence": 0.95}}]

Document text:
{truncated_text}

Facts:"""

            # Call LLM with vLLM OpenAI-compatible API
            payload = {
                "model": LLM_MODEL,
                "prompt": prompt,
                "max_tokens": 500,
                "temperature": 0.1,
            }
            
            response = requests.post(
                f"{LLM_URL}/v1/completions",
                json=payload,
                timeout=30,
            )
            response.raise_for_status()
            
            result = response.json()
            completion_text = result.get("choices", [{}])[0].get("text", "")
            
            # Parse JSON from LLM response
            json_match = re.search(r"\[.*\]", completion_text, re.DOTALL)
            if not json_match:
                logger.warning("No JSON found in LLM response")
                return []
            
            inferences = json.loads(json_match.group())
            
            # Validate and annotate
            validated = []
            for inf in inferences:
                if isinstance(inf, dict) and "fact" in inf:
                    validated.append({
                        "fact": inf.get("fact", ""),
                        "confidence": float(inf.get("confidence", 0.5)),
                        "source": "llm",
                    })
            
            logger.info(f"Extracted {len(validated)} inferences from LLM")
            return validated
            
        except requests.RequestException as e:
            logger.warning(f"LLM call failed: {e}")
            return []
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse LLM response: {e}")
            return []
        except Exception as e:
            logger.error(f"Error extracting inferences: {e}")
            return []

    def process(self, ch, method, properties, body):
        start_time = time.time()
        job_id = None

        try:
            message = json.loads(body)
            job_id = message.get("job_id")

            logger.info(f"Processing inferences for job: {job_id}")

            # Get text from Redis
            text = self.redis_client.get(f"orchestrator:job:{job_id}:text")
            if not text:
                logger.warning(f"No text found in Redis for job: {job_id}")
                jobs_total.labels(status="no_text").inc()
                ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
                return

            # Extract inferences (uses env var LLM_URL)
            inferences = self.extract_inferences(text)

            # Store in Redis
            inferences_key = f"orchestrator:job:{job_id}:micro_inferences"
            self.redis_client.set(inferences_key, json.dumps(inferences))

            # Mark step as completed
            self.redis_client.hset(
                f"orchestrator:job:{job_id}:steps", "inferences", "completed"
            )

            self.event_bus.publish_job_progress(job_id, 80, "inferences")

            duration = time.time() - start_time
            job_duration.observe(duration)
            jobs_total.labels(status="success").inc()

            logger.info(
                f"Inferences completed for job: {job_id} in {duration:.2f}s, "
                f"extracted {len(inferences)} inferences"
            )

            ch.basic_ack(delivery_tag=method.delivery_tag)

        except Exception as e:
            logger.error(f"Error processing inferences: {e}")
            jobs_total.labels(status="error").inc()
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)


def signal_handler(signum, frame):
    logger.info("Received shutdown signal, stopping worker...")
    sys.exit(0)


def main():
    import signal
    
    logger.info("Starting Inference Worker")
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    start_http_server(METRICS_PORT)
    logger.info(f"Metrics server started on port {METRICS_PORT}")

    worker = InferenceWorker()

    while True:
        try:
            with connect_rabbitmq(RABBITMQ_URL) as (connection, channel):
                logger.info(f"Consuming from queue: {QUEUE_NAME}")

                declare_queue(channel, QUEUE_NAME)
                
                # Set prefetch count for backpressure control
                prefetch_count = int(os.getenv("PREFETCH_COUNT", "3"))
                channel.basic_qos(prefetch_count=prefetch_count)
                logger.info(f"Set prefetch_count to {prefetch_count}")
                
                channel.basic_consume(
                    queue=QUEUE_NAME, on_message_callback=worker.process, auto_ack=False
                )

                channel.start_consuming()

        except Exception as e:
            logger.error(f"RabbitMQ connection error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
