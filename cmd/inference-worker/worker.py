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

    @staticmethod
    def _extract_outermost_array(text: str) -> Optional[str]:
        """
        Extract the outermost JSON array from text by counting brackets.
        Handles nested arrays within objects (e.g., entities field).
        
        Args:
            text: Text potentially containing a JSON array
            
        Returns:
            The complete outermost JSON array string, or None if not found
        """
        start = text.find("[")
        if start == -1:
            return None
        depth = 0
        for i, char in enumerate(text[start:], start):
            if char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        return None

    def extract_inferences(
        self,
        chunk_text: str,
        entities: List[Dict[str, Any]],
        source_type: str,
        max_inferences: int = 8,
    ) -> List[Dict[str, Any]]:
        """
        Extract micro-inferences from chunk text, guided by detected entities.
        
        Args:
            chunk_text: Text of the chunk (not truncated)
            entities: List of entities detected in this chunk
            source_type: Type of document source (notariado, catastro, etc)
            max_inferences: Maximum number of inferences to extract
            
        Returns:
            List of {"text": str, "confidence": float, "entities": [str]}
        """
        if not LLM_URL:
            logger.warning("No LLM_URL configured, skipping inferences")
            return []

        try:
            # Build entity reference string for prompt context
            entity_texts = [e.get("text", "") for e in entities]
            entities_str = ", ".join(entity_texts) if entity_texts else "(no entities detected)"
            
            # KNOWN LIMITATION: chunk_text is interpolated directly into the prompt.
            # This is safe because all documents are from trusted internal sources
            # (notariado, catastro, etc). If accepting untrusted external documents,
            # implement prompt injection safeguards (e.g., text sanitization/truncation).
            system_prompt = """You are a fact extraction specialist. Your task is to extract concrete, 
verifiable facts from text. Each fact must reference at least one detected entity. 
Respond ONLY with a valid JSON array containing objects with these exact fields:
- "text": the factual statement
- "confidence": a confidence score between 0.0 and 1.0
- "entities": list of entity names mentioned in the fact

Do NOT include any explanation, thinking, or text outside the JSON array."""

            user_prompt = f"""Extract facts from this text:

Text: {chunk_text}

Detected entities: {entities_str}

Maximum facts to extract: {max_inferences}

Respond with ONLY the JSON array (no other text):"""

            payload = {
                "model": LLM_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "max_tokens": 500,
                "temperature": 0.1,
                "chat_template_kwargs": {"enable_thinking": False},
            }
            
            response = requests.post(
                f"{LLM_URL}/v1/chat/completions",
                json=payload,
                timeout=30,
            )
            response.raise_for_status()
            
            result = response.json()
            completion_text = result.get("choices", [{}])[0].get("message", {}).get("content", "")
            
            logger.debug(f"Raw LLM response (first 500 chars): {completion_text[:500]}")
            
            # Remove markdown code blocks if present
            completion_text = re.sub(r"```.*?\n", "", completion_text, flags=re.DOTALL)
            completion_text = re.sub(r"```", "", completion_text)
            # Remove thinking tags
            completion_text = re.sub(r"</think>.*", "", completion_text, flags=re.DOTALL)
            completion_text = re.sub(r"<think>.*?</think>", "", completion_text, flags=re.DOTALL)
            
            # Extract the outermost JSON array (handles nested arrays correctly)
            json_str = self._extract_outermost_array(completion_text)
            if not json_str:
                response_preview = completion_text[:300].replace("\n", " ") if completion_text else "(empty)"
                logger.warning(f"No JSON array found in LLM response. Response preview: {response_preview}")
                return []
            
            logger.debug(f"Extracted JSON array (first 400 chars): {json_str[:400]}")
            
            try:
                inferences = json.loads(json_str)
                logger.info(f"Successfully parsed {len(inferences)} inferences from LLM response")
            except json.JSONDecodeError as e:
                response_preview = completion_text[:500].replace("\n", " ")
                logger.warning(f"Failed to parse LLM response JSON: {e.msg}. Response: {response_preview}")
                return []
            
            # Validate and annotate
            validated = []
            for inf in inferences:
                if isinstance(inf, dict) and "text" in inf:
                    validated.append({
                        "text": inf.get("text", ""),
                        "confidence": float(inf.get("confidence", 0.5)),
                        "entities": inf.get("entities", []),
                    })
            
            logger.info(f"Extracted {len(validated)} inferences from chunk")
            return validated
            
        except requests.RequestException as e:
            logger.warning(f"LLM call failed: {e}")
            return []
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse LLM response JSON: {e}")
            return []
        except Exception as e:
            logger.error(f"Error extracting inferences: {e}")
            return []

    def process(self, ch, method, properties, body):
        start_time = time.time()
        job_id = None
        chunk_id = None

        try:
            message = json.loads(body)
            job_id = message.get("job_id")
            chunk_id = message.get("chunk_id")
            chunk_text = message.get("chunk_text", "")
            entities = message.get("entities", [])
            source_type = message.get("source_type", "generico")
            total_chunks = message.get("total_chunks", 1)

            logger.info(f"Processing inferences for job: {job_id}, chunk: {chunk_id}")

            if not chunk_text:
                logger.warning(f"No text in message for job: {job_id}, chunk: {chunk_id}")
                jobs_total.labels(status="no_text").inc()
                ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
                return

            # Extract inferences with entity context (no truncation)
            inferences = self.extract_inferences(
                chunk_text=chunk_text,
                entities=entities,
                source_type=source_type
            )

            # Build result for this chunk
            chunk_result = {
                "chunk_id": chunk_id,
                "inferences": inferences,
            }

            # Append to Redis list (intermediate storage)
            inferences_raw_key = f"orchestrator:job:{job_id}:micro_inferences_raw"
            self.redis_client.rpush(inferences_raw_key, json.dumps(chunk_result))
            # TTL safety net: if assembly never completes, clean up after 24h
            self.redis_client.expire(inferences_raw_key, 86400)

            # Decrement atomic counter
            remaining_key = f"orchestrator:job:{job_id}:inferences:remaining"
            remaining = self.redis_client.decr(remaining_key)

            logger.info(
                f"Inference completed for job: {job_id}, chunk: {chunk_id}, "
                f"inferences: {len(inferences)}, remaining chunks: {remaining}"
            )

            # If this is the last chunk (remaining <= 0), assemble final result
            if remaining <= 0:
                logger.info(f"All inferences complete for job {job_id}, assembling results...")
                
                # Assembly lock: prevents double-assembly on RabbitMQ message redelivery.
                # SETNX is atomic — only the first caller acquires the lock.
                assembly_lock_key = f"orchestrator:job:{job_id}:inferences:assembly_lock"
                acquired = self.redis_client.setnx(assembly_lock_key, "1")
                self.redis_client.expire(assembly_lock_key, 3600)
                
                if not acquired:
                    logger.warning(
                        f"Assembly lock already held for job {job_id}, skipping duplicate assembly"
                    )
                    jobs_total.labels(status="chunk_processed").inc()
                    ch.basic_ack(delivery_tag=method.delivery_tag)
                    return
                
                try:
                    # Get all intermediate results
                    raw_results = self.redis_client.lrange(inferences_raw_key, 0, -1)
                    
                    # Parse and assemble
                    assembled = []
                    for raw_json in raw_results:
                        try:
                            chunk_data = json.loads(raw_json)
                            assembled.append(chunk_data)
                        except json.JSONDecodeError as e:
                            logger.warning(f"Failed to parse intermediate result: {e}")
                            continue
                    
                    # Sort by chunk_id for deterministic ordering
                    assembled.sort(key=lambda x: x.get("chunk_id") or 0)
                    
                    # Store final result
                    final_key = f"orchestrator:job:{job_id}:micro_inferences"
                    self.redis_client.set(final_key, json.dumps(assembled))
                    
                    # Clean up intermediate keys
                    self.redis_client.delete(inferences_raw_key)
                    self.redis_client.delete(remaining_key)
                    
                    # Mark step as completed
                    self.redis_client.hset(
                        f"orchestrator:job:{job_id}:steps", "inferences", "completed"
                    )
                    
                    # Publish progress
                    self.event_bus.publish_job_progress(job_id, 80, "inferences")
                    
                    logger.info(
                        f"Inferences finalized for job: {job_id}, "
                        f"total chunks: {len(assembled)}, "
                        f"total inferences: {sum(len(c['inferences']) for c in assembled)}"
                    )
                    
                    jobs_total.labels(status="success").inc()
                    
                except Exception as e:
                    logger.error(f"Error assembling final inferences: {e}")
                    # Mark as failed
                    self.redis_client.hset(
                        f"orchestrator:job:{job_id}:steps", "inferences", "failed"
                    )
                    jobs_total.labels(status="assembly_error").inc()
            else:
                # Not the last chunk — publish incremental progress so clients see activity
                chunks_done = total_chunks - remaining
                # remaining was already decremented; chunks_done = total - remaining
                self.event_bus.publish_job_inference_chunk_progress(
                    job_id,
                    chunks_done=chunks_done,
                    chunks_total=total_chunks,
                )
                jobs_total.labels(status="chunk_processed").inc()

            duration = time.time() - start_time
            job_duration.observe(duration)

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
