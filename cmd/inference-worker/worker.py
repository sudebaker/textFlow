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
import sys
import threading
import time
from typing import Any, Dict, List, Optional

import requests
from prometheus_client import Counter

sys.path.insert(0, "/app")
from pkg.worker_common.base import BaseWorker
from pkg.worker_common.rabbitmq import parse_rabbitmq_url

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
RAW_TTL_SECONDS = int(os.getenv("INFERENCE_RAW_TTL", "86400"))
BATCH_ENABLED = os.getenv("INFERENCE_BATCH_ENABLED", "true").lower() == "true"
BATCH_SIZE = max(2, min(10, int(os.getenv("INFERENCE_BATCH_SIZE", "3"))))
BATCH_TIMEOUT_MS = max(100, min(2000, int(os.getenv("INFERENCE_BATCH_TIMEOUT_MS", "500"))))
MAX_CHUNK_WORDS = int(os.getenv("MAX_CHUNK_WORDS", "5000"))
LLM_TIMEOUT = int(os.getenv("INFERENCE_LLM_TIMEOUT", "60"))
LLM_RETRIES = int(os.getenv("INFERENCE_LLM_RETRIES", "2"))
LLM_RETRY_BACKOFF = float(os.getenv("INFERENCE_LLM_RETRY_BACKOFF", "2.0"))


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

    def _cache_key(self, chunk_text: str, source_type: str) -> str:
        content = (
            f"{chunk_text}:{source_type}:{self._llm_model_id or 'unknown'}:"
            f"{MIN_CONFIDENCE_THRESHOLD}:"
            f"{MAX_INFERENCES_SHORT}:{MAX_INFERENCES_MEDIUM}:{MAX_INFERENCES_LONG}"
        )
        return f"inference:cache:{hashlib.sha256(content.encode()).hexdigest()}"

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
        payload = {
            "model": self._llm_model_id,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.1,
            "chat_templatekwargs": {"enable_thinking": False},
        }
        last_err = None
        for attempt in range(LLM_RETRIES):
            try:
                resp = requests.post(
                    f"{LLM_URL}/v1/chat/completions",
                    json=payload,
                    timeout=LLM_TIMEOUT,
                )
                resp.raise_for_status()
                result = resp.json()
                return result.get("choices", [{}])[0].get("message", {}).get("content", "")
            except Exception as e:
                last_err = e
                if attempt < LLM_RETRIES - 1:
                    time.sleep(LLM_RETRY_BACKOFF * (2 ** attempt))
        logger.warning(f"LLM call failed after {LLM_RETRIES} attempts: {last_err}")
        return None

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

        system_prompt = """You are a precise fact-extraction engine. Your task is to distill the key facts from a text passage into concise, self-contained statements.

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

        user_prompt = f"""Extract the {dynamic_max} MOST IMPORTANT facts from this text. Quality over quantity — only include facts with high confidence. Synthesize — do NOT copy sentences.

Text:
{chunk_text}

Respond with ONLY the JSON array:"""

        max_tokens = max(200, min((self._llm_max_model_len or 4096) - 900, 4096))
        completion_text = self._call_llm(
            [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
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
        self._set_cached(cache_key, inferences if isinstance(inferences, list) else [])
        return validated

    def process_message(self, message: Dict) -> Dict:
        if BATCH_ENABLED:
            return self._process_batch_message(message)
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
        remaining = self.redis_client.decr(remaining_key)

        if remaining <= 0:
            self._assemble_final_results(job_id)
        else:
            chunks_done = total_chunks - remaining
            self.event_bus.publish_job_inference_chunk_progress(
                job_id, chunks_done=chunks_done, chunks_total=total_chunks
            )

        return {"inferences": inferences, "remaining": remaining}

    def _process_batch_message(self, message: Dict) -> Dict:
        with self._batch_lock:
            self._batch_buffer.append(message)
            if len(self._batch_buffer) >= BATCH_SIZE:
                batch = self._batch_buffer[:]
                self._batch_buffer.clear()
                self._process_batch(batch)
                return {"batch_processed": len(batch)}
            return {"buffered": len(self._batch_buffer)}

    def flush_batch_buffer(self):
        with self._batch_lock:
            if not self._batch_buffer:
                return
            batch = self._batch_buffer[:]
            self._batch_buffer.clear()
        if batch:
            self._process_batch(batch)

    def _process_batch(self, batch: List[Dict]):
        for item in batch:
            msg = item
            job_id = msg.get("job_id")
            chunk_id = msg.get("chunk_id")
            chunk_text = msg.get("chunk_text", "")
            source_type = msg.get("source_type", "generico")
            total_chunks = msg.get("total_chunks", 1)

            if not chunk_text or len(chunk_text.split()) > MAX_CHUNK_WORDS:
                self._store_empty_result(job_id, chunk_id, total_chunks)
                continue

            cache_key = self._cache_key(chunk_text, source_type)
            cached = self._get_cached(cache_key)
            if cached is not None:
                inferences = self._validate_cached(cached)
            else:
                inferences = self.extract_inferences(chunk_text, [], source_type)
                raw = inferences
                self._set_cached(cache_key, raw)

            raw_key = f"orchestrator:job:{job_id}:micro_inferences_raw"
            self.redis_client.rpush(raw_key, json.dumps({"chunk_id": chunk_id, "inferences": inferences}))
            self.redis_client.expire(raw_key, RAW_TTL_SECONDS)

            remaining_key = f"orchestrator:job:{job_id}:inferences:remaining"
            remaining = self.redis_client.decr(remaining_key)

            if remaining <= 0:
                self._assemble_final_results(job_id)
            else:
                chunks_done = total_chunks - remaining
                self.event_bus.publish_job_inference_chunk_progress(
                    job_id, chunks_done=chunks_done, chunks_total=total_chunks
                )

    def _store_empty_result(self, job_id: str, chunk_id: Any, total_chunks: int):
        raw_key = f"orchestrator:job:{job_id}:micro_inferences_raw"
        self.redis_client.rpush(raw_key, json.dumps({"chunk_id": chunk_id, "inferences": []}))
        self.redis_client.expire(raw_key, RAW_TTL_SECONDS)
        remaining_key = f"orchestrator:job:{job_id}:inferences:remaining"
        remaining = self.redis_client.decr(remaining_key)
        if remaining <= 0:
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

        import threading
        from prometheus_client import start_http_server
        metrics_thread = threading.Thread(target=start_http_server, args=(self.metrics_port,), daemon=True)
        metrics_thread.start()

        from pkg.worker_common.rabbitmq import connect_rabbitmq, declare_queue
        while not self._shutdown_requested:
            try:
                with connect_rabbitmq(self.rabbitmq_url) as (connection, channel):
                    self._rabbitmq_connected = True
                    self._channel = channel
                    channel.queue_declare(queue=self.queue_name, durable=True)
                    prefetch_count = int(os.getenv("PREFETCH_COUNT", str(BATCH_SIZE * 2)))
                    channel.basic_qos(prefetch_count=prefetch_count)

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
                        try:
                            message = json.loads(body)
                            self._on_message(ch, method, properties, body)
                        except Exception as e:
                            self.logger.error(f"Message error: {e}")
                            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
                        finally:
                            if self._stopping:
                                ch.stop_consuming()

                    channel.basic_consume(queue=self.queue_name, on_message_callback=on_message, auto_ack=False)
                    channel.start_consuming()
            except Exception as e:
                self._rabbitmq_connected = False
                self.logger.error(f"RabbitMQ connection error: {e}")
                if not self._shutdown_requested:
                    time.sleep(5)

        self.logger.info(f"{self.worker_name} shutdown complete")


if __name__ == "__main__":
    worker = InferenceWorker()
    worker.run()
