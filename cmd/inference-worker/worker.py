#!/usr/bin/env python3
"""
Inference Worker for textFlow

Extracts micro-inferences from document chunks using an LLM (vLLM API).
Supports both batch mode (multiple chunks per LLM call) and individual mode.

Key features:
    - Batch processing: ACCUMULATE messages and process together (BATCH_SIZE, timeout)
    - LLM caching: SHA256 cache key across chunk text + source_type + model + config
    - Assembly lock: SETNX prevents double-assembly on RabbitMQ redelivery
    - Graceful degradation: Returns [] if no LLM configured
    - JSON extraction: Bracket counting handles nested arrays in LLM responses
"""

import hashlib
import json
import logging
import os
import re
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

import requests
from prometheus_client import Counter, Gauge

sys.path.insert(0, "/app")
from pkg.worker_common.base import BaseWorker
from pkg.worker_common.rabbitmq import parse_rabbitmq_url
from adaptive_semaphore import AdaptiveSemaphore

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://rabbitmq:5672/")
QUEUE_NAME = os.getenv("QUEUE_NAME", "inferences")
METRICS_PORT = int(os.getenv("METRICS_PORT", "8006"))
LLM_URL = os.getenv("LLM_URL", "")
LLM_MODEL = os.getenv("LLM_MODEL", "")
MAX_INFERENCES_SHORT = int(os.getenv("MAX_INFERENCES_SHORT", "1"))
MAX_INFERENCES_MEDIUM = int(os.getenv("MAX_INFERENCES_MEDIUM", "2"))
MAX_INFERENCES_LONG = int(os.getenv("MAX_INFERENCES_LONG", "3"))
MIN_CONFIDENCE_THRESHOLD = float(os.getenv("MIN_CONFIDENCE_THRESHOLD", "0.7"))
CACHE_TTL_SECONDS = int(os.getenv("INFERENCE_CACHE_TTL", "86400"))
CACHE_ENABLED = os.getenv("INFERENCE_CACHE_ENABLED", "true").lower() == "true"
CACHE_VERSION = os.getenv("INFERENCE_CACHE_VERSION", "1")
RAW_TTL_SECONDS = int(os.getenv("INFERENCE_RAW_TTL", "86400"))
BATCH_ENABLED = os.getenv("INFERENCE_BATCH_ENABLED", "true").lower() == "true"
BATCH_SIZE = max(2, min(10, int(os.getenv("INFERENCE_BATCH_SIZE", "3"))))
BATCH_TIMEOUT_MS = max(100, min(2000, int(os.getenv("INFERENCE_BATCH_TIMEOUT_MS", "500"))))
MAX_CHUNK_WORDS = int(os.getenv("MAX_CHUNK_WORDS", "5000"))
LLM_TIMEOUT = int(os.getenv("INFERENCE_LLM_TIMEOUT", "60"))
LLM_RETRIES = int(os.getenv("INFERENCE_LLM_RETRIES", "2"))
LLM_RETRY_BACKOFF = float(os.getenv("INFERENCE_LLM_RETRY_BACKOFF", "2.0"))

# Adaptive LLM concurrency (AIMD)
INFERENCE_ADAPTIVE_ENABLED = os.getenv("INFERENCE_ADAPTIVE_ENABLED", "false").lower() == "true"
INFERENCE_MAX_CONCURRENCY = int(os.getenv("INFERENCE_MAX_CONCURRENCY", "16"))
INFERENCE_MIN_CONCURRENCY = int(os.getenv("INFERENCE_MIN_CONCURRENCY", "1"))
INFERENCE_TARGET_TOKENS_PER_SEC = float(os.getenv("INFERENCE_TARGET_TOKENS_PER_SEC", "10.0"))
INFERENCE_TIMEOUT_DECAY_FACTOR = int(os.getenv("INFERENCE_TIMEOUT_DECAY_FACTOR", "2"))
INFERENCE_COOLDOWN_SECONDS = float(os.getenv("INFERENCE_COOLDOWN_SECONDS", "30"))
INFERENCE_CONSECUTIVE_ERRORS_FOR_COOLDOWN = int(os.getenv("INFERENCE_CONSECUTIVE_ERRORS_FOR_COOLDOWN", "5"))
LLM_TEMPERATURE = float(os.getenv("INFERENCE_LLM_TEMPERATURE", "0.1"))

_ASSEMBLY_DECR_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if not current or tonumber(current) <= 0 then
    return -1
end
return redis.call('DECR', KEYS[1])
"""

_INFERENCE_SYSTEM_PROMPT = """You are a precise fact-extraction engine. Your task is to distill the key facts from a text passage into concise, self-contained statements.

Rules:
- Each fact MUST be a SYNTHESIZED, CONDENSED statement — never copy a literal sentence from the text.
- Each fact must be independently understandable without reading the original text.
- Mention specific values, names, dates, or amounts whenever they are in the text.
- Do NOT include vague or generic statements (e.g. "the document describes...").
- Respond ONLY with a valid JSON array. No explanation. No text outside the JSON.

Each object in the array must have exactly these fields:
- "text": condensed factual statement (your own words, not copied)
- "confidence": float between 0.0 and 1.0
- "entity_refs": list of entity name strings referenced in the fact"""


class InferenceWorker(BaseWorker):
    def __init__(self):
        super().__init__(
            worker_name="inference-worker",
            queue_name=QUEUE_NAME,
            metrics_port=METRICS_PORT,
            requires_gpu=False,
        )
        self.batch_counter = Counter(
            "inference_worker_batch_total", "Batch operations", ["type"]
        )
        self._batch_buffer: List[Dict[str, Any]] = []
        self._batch_lock = threading.Lock()
        self._llm_model_id, self._llm_max_model_len = self._discover_model(LLM_URL)
        if not self._llm_model_id and LLM_MODEL:
            logger.info(f"Model discovery failed, falling back to LLM_MODEL: {LLM_MODEL}")
            self._llm_model_id = LLM_MODEL
            self._llm_max_model_len = None

        # Hash of LLM-affecting parameters for cache invalidation
        self._llm_params_hash = self._compute_llm_params_hash()

        # Adaptive LLM concurrency
        self._semaphore: Optional[AdaptiveSemaphore] = None
        if INFERENCE_ADAPTIVE_ENABLED and LLM_URL:
            self._semaphore = AdaptiveSemaphore(
                min_concurrency=INFERENCE_MIN_CONCURRENCY,
                max_concurrency=INFERENCE_MAX_CONCURRENCY,
                target_tokens_per_sec=INFERENCE_TARGET_TOKENS_PER_SEC,
                decay_factor=INFERENCE_TIMEOUT_DECAY_FACTOR,
                cooldown_seconds=INFERENCE_COOLDOWN_SECONDS,
                consecutive_errors_for_cooldown=INFERENCE_CONSECUTIVE_ERRORS_FOR_COOLDOWN,
            )
            logger.info(
                f"Adaptive semaphore enabled: min={INFERENCE_MIN_CONCURRENCY}, "
                f"max={INFERENCE_MAX_CONCURRENCY}, target_tps={INFERENCE_TARGET_TOKENS_PER_SEC}"
            )

        # Lua script for atomic remaining counter decrement
        self._assembly_decr = self.redis_client.register_script(_ASSEMBLY_DECR_SCRIPT)

        # Prometheus metrics
        self._cwnd_gauge = Gauge("inference_worker_cwnd", "Current congestion window")
        self._in_flight_gauge = Gauge("inference_worker_in_flight", "LLM calls in flight")
        self._total_requests_counter = Counter("inference_worker_llm_requests_total", "Total LLM requests")
        self._total_timeouts_counter = Counter("inference_worker_llm_timeouts_total", "LLM timeouts")
        self._avg_tps_gauge = Gauge("inference_worker_llm_avg_tokens_per_sec", "Average tokens/sec")
        self._cooldown_gauge = Gauge("inference_worker_cooldown", "1 if circuit breaker active")

        # Graceful shutdown
        self._channel = None
        self._connection = None
        self._stopping = False
        self._executor: Optional[ThreadPoolExecutor] = None
        self._metrics_thread: Optional[threading.Thread] = None
        signal.signal(signal.SIGTERM, self._handle_sigterm)

        # Export initial semaphore state so gauges aren't stuck at 0
        self._export_metrics()

    def _discover_model(self, llm_url: str) -> tuple:
        if not llm_url:
            return (None, None)
        try:
            resp = requests.get(f"{llm_url}/v1/models", timeout=5)
            resp.raise_for_status()
            models = resp.json()
            if not models.get("data"):
                return (None, None)
            info = models["data"][0]
            return (info.get("id"), info.get("max_model_len", 4096))
        except Exception as e:
            logger.warning(f"Model discovery failed: {e}")
            return (None, None)

    def _handle_sigterm(self, signum, frame):
        """Handle SIGTERM for graceful shutdown."""
        logger.info("SIGTERM received, initiating graceful shutdown...")
        self._stopping = True
        if self._channel:
            try:
                self._channel.stop_consuming()
            except Exception:
                pass

        # Wait for thread pool to finish in-flight LLM requests
        if self._executor:
            timeout = LLM_TIMEOUT + 30
            self._executor.shutdown(wait=True, cancel_futures=False)
            logger.info("Thread pool shut down")
        else:
            logger.info("No executor, skipping drain")

    def _compute_llm_params_hash(self) -> str:
        params = json.dumps({
            "v": CACHE_VERSION,
            "sp": _INFERENCE_SYSTEM_PROMPT,
            "tp": {"enable_thinking": False},
            "temp": LLM_TEMPERATURE,
            "model": self._llm_model_id or "unknown",
            "threshold": MIN_CONFIDENCE_THRESHOLD,
            "max": [MAX_INFERENCES_SHORT, MAX_INFERENCES_MEDIUM, MAX_INFERENCES_LONG],
        }, sort_keys=True)
        return hashlib.sha256(params.encode()).hexdigest()[:16]

    def _cache_key(self, chunk_text: str, source_type: str) -> str:
        text_hash = hashlib.sha256(f"{chunk_text}:{source_type}".encode()).hexdigest()
        return f"inference:cache:{self._llm_params_hash}:{text_hash}"

    def _get_cached(self, cache_key: str) -> Optional[List[Dict]]:
        if not CACHE_ENABLED:
            return None
        try:
            cached = self.redis_client.get(cache_key)
            if cached:
                return json.loads(cached)
        except Exception:
            pass
        return None

    def _set_cached(self, cache_key: str, inferences: List[Dict]) -> None:
        if not CACHE_ENABLED:
            return
        try:
            self.redis_client.setex(cache_key, CACHE_TTL_SECONDS, json.dumps(inferences))
        except Exception:
            pass

    def _validate_cached(self, cached: List[Dict]) -> List[Dict]:
        validated = []
        for inf in cached:
            if isinstance(inf, dict) and "text" in inf:
                conf = float(inf.get("confidence", 0.5))
                if conf >= MIN_CONFIDENCE_THRESHOLD:
                    validated.append({
                        "text": inf.get("text", ""),
                        "confidence": conf,
                        "entity_refs": inf.get("entity_refs", []),
                    })
        return validated

    def _extract_outermost_array(self, text: str) -> Optional[str]:
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
                    return text[start:i + 1]
        return None

    def _dynamic_max_inferences(self, word_count: int) -> int:
        if word_count < 200:
            return MAX_INFERENCES_SHORT
        if word_count < 500:
            return MAX_INFERENCES_MEDIUM
        return MAX_INFERENCES_LONG

    def _call_llm(self, messages: List[Dict], max_tokens: int) -> Optional[str]:
        if not LLM_URL or not self._llm_model_id:
            return None

        # Acquire token from adaptive semaphore
        # Timeout accounts for worst case: other chunks in batch holding the semaphore
        if self._semaphore:
            semaphore_timeout = LLM_TIMEOUT * LLM_RETRIES * BATCH_SIZE + 10
            if not self._semaphore.acquire(timeout=semaphore_timeout):
                logger.warning("Adaptive semaphore acquire timeout")
                self._export_metrics()
                return None

        payload = {
            "model": self._llm_model_id,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": LLM_TEMPERATURE,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        t0 = time.monotonic()
        released = False
        try:
            for attempt in range(LLM_RETRIES):
                try:
                    resp = requests.post(
                        f"{LLM_URL}/v1/chat/completions",
                        json=payload,
                        timeout=LLM_TIMEOUT,
                    )
                    resp.raise_for_status()
                    result = resp.json()
                    content = result.get("choices", [{}])[0].get("message", {}).get("content", "")

                    # Calculate tokens/sec
                    latency_ms = (time.monotonic() - t0) * 1000
                    usage = result.get("usage", {})
                    completion_tokens = usage.get("completion_tokens", 0)
                    tokens_per_sec = (completion_tokens / latency_ms * 1000) if latency_ms > 0 else 0

                    # Release semaphore (exactly once)
                    if self._semaphore and not released:
                        released = True
                        self._semaphore.release(
                            latency_ms=latency_ms,
                            tokens_per_sec=tokens_per_sec,
                            is_error=False,
                        )
                        self._total_requests_counter.inc()
                        self._export_metrics()

                    return content
                except Exception as e:
                    if attempt < LLM_RETRIES - 1:
                        time.sleep(LLM_RETRY_BACKOFF * (2 ** attempt))

            # All retries failed
            latency_ms = (time.monotonic() - t0) * 1000
            if self._semaphore and not released:
                released = True
                self._semaphore.release(
                    latency_ms=latency_ms,
                    tokens_per_sec=0,
                    is_error=True,
                )
                self._total_timeouts_counter.inc()
                self._export_metrics()
            return None
        except Exception:
            latency_ms = (time.monotonic() - t0) * 1000
            if self._semaphore and not released:
                released = True
                self._semaphore.release(
                    latency_ms=latency_ms,
                    tokens_per_sec=0,
                    is_error=True,
                )
                self._total_timeouts_counter.inc()
                self._export_metrics()
            raise

    def _export_metrics(self):
        """Export adaptive semaphore metrics to Prometheus."""
        if not self._semaphore:
            return
        stats = self._semaphore.get_stats()
        self._cwnd_gauge.set(stats["cwnd"])
        self._in_flight_gauge.set(stats["in_flight"])
        self._total_requests_counter.inc(0)  # ensure counter is registered (no-op on first call)
        self._total_timeouts_counter.inc(0)
        self._avg_tps_gauge.set(stats["avg_tokens_per_sec"])
        self._cooldown_gauge.set(1 if stats["is_in_cooldown"] else 0)

    def _metrics_loop(self):
        """Periodically export semaphore metrics (every 5s)."""
        while not self._stopping:
            self._export_metrics()
            time.sleep(5)

    def extract_inferences(
        self, chunk_text: str, entities: List, source_type: str
    ) -> List[Dict]:
        if not LLM_URL or not self._llm_model_id:
            return []

        cache_key = self._cache_key(chunk_text, source_type)
        cached = self._get_cached(cache_key)
        if cached is not None:
            return self._validate_cached(cached)

        word_count = len(chunk_text.split())
        dynamic_max = self._dynamic_max_inferences(word_count)

        user_prompt = f"""Extract the {dynamic_max} MOST IMPORTANT facts from this text. Quality over quantity — only include facts with high confidence. Synthesize — do NOT copy sentences.

Text:
{chunk_text}

Respond with ONLY the JSON array:"""

        max_tokens = max(200, min((self._llm_max_model_len or 4096) - 900, 4096))
        completion_text = self._call_llm(
            [{"role": "system", "content": _INFERENCE_SYSTEM_PROMPT}, {"role": "user", "content": user_prompt}],
            max_tokens,
        )
        if not completion_text:
            return []

        completion_text = re.sub(r"```.*?\n", "", completion_text, flags=re.DOTALL)
        completion_text = re.sub(r"```", "", completion_text)
        completion_text = re.sub(r"<think>.*?</think>", "", completion_text, flags=re.DOTALL)

        json_str = self._extract_outermost_array(completion_text)
        if not json_str:
            return []

        try:
            inferences = json.loads(json_str)
        except json.JSONDecodeError:
            return []

        validated = []
        for inf in inferences:
            if isinstance(inf, dict) and "text" in inf:
                validated.append({
                    "text": inf.get("text", ""),
                    "confidence": float(inf.get("confidence", 0.5)),
                    "entity_refs": inf.get("entity_refs", inf.get("entities", [])),
                })
        validated = [i for i in validated if i["confidence"] >= MIN_CONFIDENCE_THRESHOLD]
        self._set_cached(cache_key, validated)
        return validated

    def process_message(self, message: Dict) -> Dict:
        """Single-message processing entry point used by BaseWorker."""
        return self._process_single_message(message)

    def _process_single_message(self, message: Dict) -> Dict:
        job_id = message.get("job_id")
        chunk_id = message.get("chunk_id")
        chunk_text = message.get("chunk_text", "")
        entities = message.get("entities", [])
        source_type = message.get("source_type", "generico")
        total_chunks = message.get("total_chunks", 1)

        logger.info(f"Processing inferences for job: {job_id}, chunk: {chunk_id}")

        if not chunk_text:
            raise ValueError(f"No text in message for job: {job_id}, chunk: {chunk_id}")

        word_count = len(chunk_text.split())
        if word_count > MAX_CHUNK_WORDS:
            self._store_empty_result(job_id, chunk_id, total_chunks)
            return {"skipped": True, "reason": "chunk_too_large"}

        inferences = self.extract_inferences(chunk_text, entities, source_type)

        chunk_result = {"chunk_id": chunk_id, "inferences": inferences}
        raw_key = f"orchestrator:job:{job_id}:micro_inferences_raw"
        self.redis_client.rpush(raw_key, json.dumps(chunk_result))
        self.redis_client.expire(raw_key, RAW_TTL_SECONDS)

        remaining_key = f"orchestrator:job:{job_id}:inferences:remaining"
        remaining = self._assembly_decr(keys=[remaining_key])

        if remaining < 0:
            # Counter missing or already <= 0 — job already assembled or cleared
            return {"inferences": inferences, "remaining": 0}

        if remaining == 0:
            self._assemble_final_results(job_id)
        else:
            chunks_done = total_chunks - remaining
            self.event_bus.publish_job_inference_chunk_progress(
                job_id, chunks_done=chunks_done, chunks_total=total_chunks
            )

        return {"inferences": inferences, "remaining": remaining}

    def _process_batch_message(self, item: Dict) -> None:
        """Buffer a single message for batch processing. Called from run() batch mode."""
        with self._batch_lock:
            self._batch_buffer.append(item)
            if len(self._batch_buffer) >= BATCH_SIZE:
                batch = self._batch_buffer[:]
                self._batch_buffer.clear()
                self._flush_batch_buffer(batch)

    def flush_batch_buffer(self):
        """Public API to flush any buffered messages (e.g. on shutdown)."""
        with self._batch_lock:
            if not self._batch_buffer:
                return
            batch = self._batch_buffer[:]
            self._batch_buffer.clear()
        self._flush_batch_buffer(batch)

    def _flush_batch_buffer(self, batch: List[Dict]):
        """Submit each message to thread pool for concurrent LLM processing.

        Each message is processed independently. ACK/NACK is scheduled back
        to the main thread via add_callback_threadsafe (pika is not thread-safe).
        """
        if not batch:
            return

        def _process_one(item):
            """Process a single message. Runs in thread pool."""
            try:
                msg = item["message"]
                self._process_single_message(msg)
                self._schedule_ack(item["ch"], item["method"])
            except Exception as e:
                logger.error(f"Message processing failed: {e}")
                self._schedule_nack(item["ch"], item["method"])

        for item in batch:
            if self._executor:
                self._executor.submit(_process_one, item)
            else:
                # Fallback: process synchronously (no adaptive mode)
                try:
                    _process_one(item)
                except Exception:
                    pass

    def _schedule_ack(self, ch, method):
        """Schedule ACK on the main thread (pika is not thread-safe)."""
        if self._connection:
            self._connection.add_callback_threadsafe(
                lambda: ch.basic_ack(delivery_tag=method.delivery_tag)
            )

    def _schedule_nack(self, ch, method):
        """Schedule NACK on the main thread (pika is not thread-safe)."""
        if self._connection:
            self._connection.add_callback_threadsafe(
                lambda: ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            )

    def _store_empty_result(self, job_id: str, chunk_id: Any, total_chunks: int):
        raw_key = f"orchestrator:job:{job_id}:micro_inferences_raw"
        self.redis_client.rpush(raw_key, json.dumps({"chunk_id": chunk_id, "inferences": []}))
        self.redis_client.expire(raw_key, RAW_TTL_SECONDS)
        remaining_key = f"orchestrator:job:{job_id}:inferences:remaining"
        remaining = self._assembly_decr(keys=[remaining_key])
        if remaining == 0:
            self._assemble_final_results(job_id)

    def _assemble_final_results(self, job_id: str):
        lock_key = f"orchestrator:job:{job_id}:inferences:assembly_lock"
        acquired = self.redis_client.setnx(lock_key, "1")
        self.redis_client.expire(lock_key, 3600)
        if not acquired:
            logger.warning(f"Assembly lock already held for job {job_id}")
            return

        try:
            raw_key = f"orchestrator:job:{job_id}:micro_inferences_raw"
            raw_results = self.redis_client.lrange(raw_key, 0, -1)
            assembled = []
            for raw_json in raw_results:
                try:
                    assembled.append(json.loads(raw_json))
                except json.JSONDecodeError:
                    pass
            assembled.sort(key=lambda x: x.get("chunk_id") or 0)
            final_key = f"orchestrator:job:{job_id}:micro_inferences"
            self.redis_client.set(final_key, json.dumps(assembled))
            self.redis_client.delete(raw_key)
            remaining_key = f"orchestrator:job:{job_id}:inferences:remaining"
            self.redis_client.delete(remaining_key)
            self.redis_client.hset(f"orchestrator:job:{job_id}:steps", "inferences", "completed")
            self.event_bus.publish_job_progress(job_id, 80, "inferences")
            total_inferences = sum(len(c.get("inferences", [])) for c in assembled)
            logger.info(f"Inferences finalized for job: {job_id}, total inferences: {total_inferences}")
        except Exception as e:
            logger.error(f"Error assembling inferences: {e}")
            self.redis_client.hset(f"orchestrator:job:{job_id}:steps", "inferences", "failed")

    def run(self):
        if not BATCH_ENABLED:
            super().run()
            return

        self.logger.info(f"Starting Inference Worker (batch mode: size={BATCH_SIZE}, timeout={BATCH_TIMEOUT_MS}ms)")

        from prometheus_client import start_http_server
        metrics_thread = threading.Thread(target=start_http_server, args=(self.metrics_port,), daemon=True)
        metrics_thread.start()

        # Create thread pool for concurrent LLM calls
        if INFERENCE_ADAPTIVE_ENABLED and LLM_URL:
            self._executor = ThreadPoolExecutor(max_workers=INFERENCE_MAX_CONCURRENCY)
            self.logger.info(f"Thread pool created: max_workers={INFERENCE_MAX_CONCURRENCY}")

        # Start periodic metrics export thread
        if self._semaphore:
            self._metrics_thread = threading.Thread(target=self._metrics_loop, daemon=True)
            self._metrics_thread.start()
            self.logger.info("Metrics export thread started (every 5s)")

        from pkg.worker_common.rabbitmq import connect_rabbitmq
        while not self._shutdown_requested and not self._stopping:
            try:
                with connect_rabbitmq(self.rabbitmq_url) as (connection, channel):
                    self._rabbitmq_connected = True
                    self._connection = connection
                    self._channel = channel
                    channel.queue_declare(
                        queue=self.queue_name,
                        durable=True,
                        arguments={
                            "x-dead-letter-exchange": "document_processor_dlx",
                            "x-dead-letter-routing-key": f"{self.queue_name}_failed",
                        },
                    )
                    # Dynamic prefetch: aligned with adaptive semaphore cwnd
                    if self._semaphore:
                        prefetch_count = INFERENCE_MAX_CONCURRENCY + 1
                    else:
                        prefetch_count = int(os.getenv("PREFETCH_COUNT", str(BATCH_SIZE * 2)))
                    channel.basic_qos(prefetch_count=prefetch_count)
                    self.logger.info(f"Prefetch count: {prefetch_count}")

                    def schedule_flush():
                        if self._shutdown_requested:
                            return
                        try:
                            self.flush_batch_buffer()
                        except Exception as e:
                            self.logger.error(f"Flush error: {e}")
                        if not self._shutdown_requested:
                            try:
                                connection.call_later(BATCH_TIMEOUT_MS / 1000.0, schedule_flush)
                            except Exception:
                                pass

                    connection.call_later(BATCH_TIMEOUT_MS / 1000.0, schedule_flush)

                    def on_message(ch, method, properties, body):
                        if self._stopping:
                            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
                            return
                        try:
                            message = json.loads(body)
                            self._process_batch_message({
                                "ch": ch,
                                "method": method,
                                "properties": properties,
                                "body": body,
                                "message": message,
                            })
                        except Exception as e:
                            self.logger.error(f"Message error: {e}")
                            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

                    channel.basic_consume(queue=self.queue_name, on_message_callback=on_message, auto_ack=False)
                    channel.start_consuming()
            except Exception as e:
                self._rabbitmq_connected = False
                self.logger.error(f"RabbitMQ connection error: {e}")
                if not self._shutdown_requested and not self._stopping:
                    time.sleep(5)

        self.logger.info(f"{self.worker_name} shutdown complete")


if __name__ == "__main__":
    worker = InferenceWorker()
    worker.run()
