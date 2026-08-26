#!/usr/bin/env python3
"""
Inference Worker for textFlow.

Extracts micro-inferences (facts) from document chunks using an external LLM (vLLM).
This is an OPTIONAL feature — only activated when a job requests features=["inferences"].

Architecture:
    - Consumes messages from RabbitMQ inferences queue
    - For each chunk, calls vLLM /v1/chat/completions API to extract facts
    - Each fact includes confidence score and entity_refs (referenced entity names)
    - Stores raw results in Redis intermediate keys
    - Assembles final results when all chunks complete
    - Publishes progress events to Event Bus

Model Discovery:
    - Attempts to discover available LLM models at startup via vLLM /v1/models API
    - If discovery fails but LLM_MODEL env var is set, falls back to that value
    - If both methods fail, inferences are permanently disabled (returns [] for all chunks)
    - Model ID and max_model_len are cached in __init__, never re-discovered

Input (RabbitMQ Message):
    - job_id: Job identifier
    - chunk_id: Chunk identifier
    - chunk_text: Document text to analyze
    - entities: Named entities detected in this chunk (list of {text, ...})
    - source_type: Document source type (notariado, catastro, bancario, etc.)
    - total_chunks: Total number of chunks being processed

Output (Redis):
    - micro_inferences: Final assembled list of all extracted facts from all chunks
    - Each inference: {"text": str, "confidence": 0.0-1.0, "entity_refs": [str]}

Environment Variables:
    - REDIS_URL: Redis connection URL (default: redis://redis:6379)
    - RABBITMQ_URL: RabbitMQ connection URL (default: amqp://rabbitmq:5672/)
    - QUEUE_NAME: Queue name (default: inferences)
    - LLM_URL: Base URL of vLLM server (e.g., http://localhost:8000) — if empty, inferences disabled
    - LLM_MODEL: Fallback model ID (optional, used if auto-discovery fails)
    - METRICS_PORT: Prometheus metrics port (default: 8006)
    - PREFETCH_COUNT: RabbitMQ prefetch count (default: 3)
    - MAX_CHUNK_WORDS: Maximum chunk size in words before skipping (default: 5000)
    - INFERENCE_BATCH_ENABLED: Enable batch processing (default: true)
    - INFERENCE_BATCH_SIZE: Chunks per batch LLM call (default: 3, range: 2-10)
    - INFERENCE_BATCH_TIMEOUT_MS: Flush timeout in ms (default: 500)
    - INFERENCE_CACHE_ENABLED: Enable Redis cache (default: true)
    - INFERENCE_CACHE_TTL: Cache TTL in seconds (default: 86400)
    - INFERENCE_RAW_TTL: TTL for intermediate Redis results in seconds (default: 86400)
    - INFERENCE_LLM_TIMEOUT: LLM request timeout in seconds (default: 60)
    - INFERENCE_LLM_RETRIES: Max retries on timeout/connection error (default: 2)
    - INFERENCE_LLM_RETRY_BACKOFF: Base backoff in seconds, doubled per attempt (default: 2.0)

Key Features:
    - Thinking-tag handling: Removes <think>...</think> blocks from LLM responses
    - JSON array extraction: Uses bracket counting to handle nested arrays correctly
    - Max tokens calculation: Dynamically computed from model max_model_len with 900-token overhead
    - Assembly lock: SETNX-based lock prevents double-assembly on message redelivery
    - Graceful degradation: If LLM unavailable, returns empty inference list
    - Prompt safety: Chunk text is trusted internal source (notariado, catastro, etc.)
"""

import os
import sys
import json
import time
import re
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Any, Optional

import redis
import pika
import requests

sys.path.insert(0, "/app")
from pkg.worker_common.base import BaseWorker
from pkg.worker_common.rabbitmq import parse_rabbitmq_url

# Note: adaptive_semaphore lives in the same directory as this worker.
from adaptive_semaphore import AdaptiveSemaphore

QUEUE_NAME = os.getenv("QUEUE_NAME", "inferences")
METRICS_PORT = int(os.getenv("METRICS_PORT", "8006"))
LLM_URL = os.getenv("LLM_URL", "")
LLM_MODEL = os.getenv("LLM_MODEL", "")
MAX_INFERENCES_SHORT   = int(os.getenv("MAX_INFERENCES_SHORT",   "1"))  # chunks < 200 words
MAX_INFERENCES_MEDIUM  = int(os.getenv("MAX_INFERENCES_MEDIUM",  "2"))  # chunks 200-499 words
MAX_INFERENCES_LONG    = int(os.getenv("MAX_INFERENCES_LONG",    "3"))  # chunks >= 500 words
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

# Adaptive LLM concurrency (Fase 3). Behind INFERENCE_ADAPTIVE_ENABLED so the
# current behavior is unchanged until GPU benchmarks are available.
ADAPTIVE_ENABLED = os.getenv("INFERENCE_ADAPTIVE_ENABLED", "false").lower() == "true"
ADAPTIVE_MAX_CONCURRENCY = int(os.getenv("INFERENCE_MAX_CONCURRENCY", "16"))
ADAPTIVE_MIN_CONCURRENCY = int(os.getenv("INFERENCE_MIN_CONCURRENCY", "1"))
ADAPTIVE_DECAY_FACTOR = int(os.getenv("INFERENCE_TIMEOUT_DECAY_FACTOR", "2"))
ADAPTIVE_COOLDOWN_SECONDS = float(os.getenv("INFERENCE_COOLDOWN_SECONDS", "30"))
ADAPTIVE_CONSECUTIVE_ERRORS = int(os.getenv("INFERENCE_CONSECUTIVE_ERRORS_FOR_COOLDOWN", "5"))

# Global shutdown flag. Set True by signal_handler/main. Defined here (not just
# inside main) so _process_single's finally block can reference it even when
# main() has not run yet (e.g. in unit tests).
_stopping = False


class _EmptyLlmResponse:
    """
    Minimal stand-in for a requests.Response used when the adaptive semaphore
    cannot grant a token. Parses as an empty LLM result so callers degrade
    gracefully to an empty inference list.
    """

    def json(self):
        return {"choices": []}


class InferenceWorker(BaseWorker):
    """
    RabbitMQ consumer that extracts micro-inferences from document chunks using an LLM.

    This worker subscribes to the inferences queue and processes chunks from extraction.    For each chunk, it calls an external vLLM API to extract factual statements guided
    by detected entities.

    Lifecycle:
        1. __init__: Connect to Redis, discover LLM model (one-time at startup), set up Event Bus
        2. process: Consume messages from RabbitMQ, extract inferences per chunk, assemble results
        3. Graceful shutdown: SIGINT/SIGTERM sets _stopping=True; consumer drains current message then stops

    State:
        - redis_client: Redis connection for storing intermediate and final results
        - event_bus: Event Bus for publishing progress updates
        - llm_model_id: Discovered or fallback LLM model ID (may be None if discovery fails)
        - llm_max_model_len: Max token length for discovered model (may be None)

    Key Behavior:
        - If llm_model_id is None, all inferences return [] (graceful degradation)
        - Raw results stored per chunk in Redis lists (micro_inferences_raw)
        - Final assembly triggered when remaining counter reaches 0 (last chunk processed)
        - Assembly lock (SETNX) prevents double-processing on message redelivery
        - Intermediate keys auto-expire after 24 hours (safety net)
    """

    def __init__(self):
        super().__init__(
            worker_name="inference-worker",
            queue_name=QUEUE_NAME,
            metrics_port=METRICS_PORT,
        )

        # Batch processing buffer
        self._batch_buffer: List[Dict[str, Any]] = []
        self._batch_lock = threading.Lock()

        # Batch-specific metric
        from prometheus_client import Counter
        self.batch_counter = Counter("inference_worker_batch_total", "Batch operations", ["type"])

        # Queue wait time (spec 1.1): messages carry queued_at (unix ms)
        from prometheus_client import Histogram
        self.queue_time = Histogram(
            "inference_worker_queue_time_seconds",
            "Time messages spend waiting in the queue before consumption",
            buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 15.0, 60.0],
        )

        # LLM throughput (spec 1.3), from usage.completion_tokens / latency.
        from prometheus_client import Gauge as _Gauge
        self.llm_tokens_per_sec = _Gauge(
            "inference_worker_llm_tokens_per_sec",
            "LLM completion tokens per second (last call)",
        )

        # Adaptive concurrency control (Fase 3). Only wired when enabled; the
        # consumer loop stays single-threaded on pika, so the semaphore gates
        # the LLM calls themselves (which block for seconds). A full
        # ThreadPoolExecutor integration requires re-designing the pika loop
        # and is deferred to the GPU benchmarks.
        self._adaptive = None
        self._executor = None
        # Adaptive counters/gauges; None when the feature is disabled so
        # _call_llm/_export_adaptive_metrics can check them unconditionally.
        self._llm_requests_counter = None
        self._llm_timeouts_counter = None
        self._cwnd_gauge = None
        self._in_flight_gauge = None
        self._cooldown_gauge = None
        if ADAPTIVE_ENABLED:
            self._adaptive = AdaptiveSemaphore(
                min_concurrency=ADAPTIVE_MIN_CONCURRENCY,
                max_concurrency=ADAPTIVE_MAX_CONCURRENCY,
                decay_factor=ADAPTIVE_DECAY_FACTOR,
                cooldown_seconds=ADAPTIVE_COOLDOWN_SECONDS,
                consecutive_errors_for_cooldown=ADAPTIVE_CONSECUTIVE_ERRORS,
            )
            # Full-concurrent design (W6): the executor runs per-chunk
            # processing tasks; pika's thread only schedules and acks via
            # connection.add_callback_threadsafe.
            self._executor = ThreadPoolExecutor(
                max_workers=ADAPTIVE_MAX_CONCURRENCY
            )
            # Live pika objects, set by main() on each (re)connect. Worker
            # threads must never touch them directly — only through
            # add_callback_threadsafe callbacks.
            self._connection = None
            self._channel = None
            # Tasks submitted to the executor and not finished yet.
            self._tasks_lock = threading.Lock()
            self._tasks_in_flight = 0
            from prometheus_client import Gauge
            self._cwnd_gauge = Gauge("inference_worker_cwnd", "Current congestion window")
            self._in_flight_gauge = Gauge("inference_worker_in_flight", "LLM calls currently in flight")
            self._cooldown_gauge = Gauge("inference_worker_cooldown", "1 if circuit breaker is active")
            self._llm_requests_counter = Counter(
                "inference_worker_llm_requests_total", "Total LLM requests"
            )
            self._llm_timeouts_counter = Counter(
                "inference_worker_llm_timeouts_total", "Total LLM timeouts"
            )
            self.logger.info(
                f"Adaptive LLM concurrency ENABLED: min={ADAPTIVE_MIN_CONCURRENCY}, "
                f"max={ADAPTIVE_MAX_CONCURRENCY}, binary AIMD (success/error)"
            )

        # Discover model at startup (one-time, cached)
        if LLM_URL:
            self.llm_model_id, self.llm_max_model_len = self._discover_model(LLM_URL)
            if not self.llm_model_id and LLM_MODEL:
                self.logger.info(
                    f"Model discovery failed, falling back to LLM_MODEL env var: {LLM_MODEL}"
                )
                self.llm_model_id = LLM_MODEL
                self.llm_max_model_len = None
        else:
            self.llm_model_id = None
            self.llm_max_model_len = None

    def _discover_model(self, llm_url: str) -> tuple[Optional[str], Optional[int]]:
        """
        Discover available models from vLLM API endpoint.

        Makes a GET request to {llm_url}/v1/models to discover running inference models.
        This discovery happens ONE-TIME at worker startup and the result is cached
        in self.llm_model_id — it is never re-discovered.

        vLLM API Contract:
            - Endpoint: GET {llm_url}/v1/models
            - Response format: {"data": [{"id": "...", "max_model_len": ...}, ...]}
            - Returns the first available model from the data array

        Args:
            llm_url (str): Base URL of vLLM server (e.g., http://localhost:8000).
                          Do NOT include /v1 path — this method appends it.

        Returns:
            tuple[Optional[str], Optional[int]]: A tuple of:
                - model_id (str): First model's ID if discovery succeeds, None otherwise
                - max_model_len (int): Model's max_model_len if available, None otherwise
            Returns (None, None) if discovery fails or llm_url is empty.

        Error Handling:
            - Catches requests.RequestException (network errors, timeouts)
            - Catches KeyError, ValueError (malformed JSON response)
            - Logs WARNING for each failure case
            - Never raises exceptions; always returns (None, None) on failure

        Timeout: 5 seconds per request
        """
        if not llm_url:
            self.logger.warning("No LLM_URL configured, model discovery skipped")
            return (None, None)

        try:
            response = requests.get(
                f"{llm_url}/v1/models",
                timeout=5,
            )
            response.raise_for_status()
            models = response.json()

            if not models.get("data"):
                self.logger.warning(f"No models found in vLLM response: {models}")
                return (None, None)

            model_info = models["data"][0]
            model_id = model_info.get("id")
            max_model_len = model_info.get("max_model_len", 4096)

            self.logger.info(
                f"Discovered model '{model_id}' with max_model_len={max_model_len}"
            )
            return (model_id, max_model_len)

        except requests.RequestException as e:
            self.logger.warning(f"Failed to discover models from {llm_url}: {e}")
            return (None, None)
        except (KeyError, ValueError) as e:
            self.logger.warning(f"Failed to parse vLLM models response: {e}")
            return (None, None)

    @staticmethod
    def _extract_outermost_array(text: str) -> Optional[str]:
        """
        Extract the outermost JSON array from text by counting bracket depth.

        This method is used because LLM responses may include explanatory text before
        or after the JSON array (markdown code blocks, thinking tags, etc.). This
        function strips all non-JSON content and returns just the array.

        Algorithm:
            1. Find the first '[' character
            2. Count bracket depth: increment on '[', decrement on ']'
            3. When depth returns to 0, we've found the complete outermost array
            4. Handles nested arrays within objects correctly (e.g., "entities" field)

        Example:
            Input: "Here are the facts: [{"text": "...", "entities": ["name1", "name2"]}] Done"
            Output: '[{"text": "...", "entities": ["name1", "name2"]}]'
            (Correctly skips nested brackets in the "entities" array)

        Args:
            text (str): Text potentially containing a JSON array among other content.

        Returns:
            Optional[str]: The complete outermost JSON array as a string (including brackets),
                          or None if no '[' character found or array is malformed.

        Error Handling:
            - Returns None if no '[' found in text
            - Returns None if brackets are unclosed (depth never returns to 0)
            - Never raises exceptions
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

    def _cache_key(self, chunk_text: str, source_type: str) -> str:
        """Generate a deterministic cache key from chunk content, source type, model, and config."""
        content = (
            f"{chunk_text}:{source_type}:{self.llm_model_id or 'unknown'}:"
            f"{MIN_CONFIDENCE_THRESHOLD}:"
            f"{MAX_INFERENCES_SHORT}:{MAX_INFERENCES_MEDIUM}:{MAX_INFERENCES_LONG}"
        )
        text_hash = hashlib.sha256(content.encode()).hexdigest()
        return f"inference:cache:{text_hash}"

    def _validate_cached_inferences(
        self, cached: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Validate and filter cached inferences by confidence threshold."""
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

    def _get_cached(self, cache_key: str) -> Optional[List[Dict[str, Any]]]:
        """Retrieve cached inferences from Redis. Returns None on miss or error."""
        if not CACHE_ENABLED:
            return None
        try:
            cached = self.redis_client.get(cache_key)
            if cached:
                self.logger.info(f"Cache HIT for {cache_key[:50]}")
                return json.loads(cached)
        except Exception as e:
            self.logger.warning(f"Cache read error: {e}")
        return None

    def _set_cached(self, cache_key: str, inferences: List[Dict[str, Any]]) -> None:
        """Store inferences in Redis cache with TTL."""
        if not CACHE_ENABLED:
            return
        try:
            self.redis_client.setex(cache_key, CACHE_TTL_SECONDS, json.dumps(inferences))
            self.logger.debug(f"Cached {len(inferences)} inferences (TTL: {CACHE_TTL_SECONDS}s)")
        except Exception as e:
            self.logger.warning(f"Cache write error: {e}")

    def _observe_queue_time(self, message: Dict[str, Any]) -> None:
        """Record queue wait from the message's queued_at stamp (unix ms)."""
        queued_at = message.get("queued_at")
        if isinstance(queued_at, (int, float)) and queued_at > 0:
            self.queue_time.observe(max(0.0, time.time() - queued_at / 1000.0))

    def _call_llm(
        self,
        payload: Dict[str, Any],
        timeout: float,
        retries: int = LLM_RETRIES,
        retry_backoff: float = LLM_RETRY_BACKOFF,
    ) -> requests.Response:
        """
        Perform an LLM request to the vLLM /v1/chat/completions endpoint.

        When ADAPTIVE_ENABLED, the request is gated by the AdaptiveSemaphore:
        a token is acquired before the request and released afterwards, feeding
        the latency/tokens-per-sec back into the congestion window so cwnd
        grows/shrinks based on actual LLM throughput. On acquire failure (or
        cooldown), returns an empty JSON response so callers degrade gracefully.

        Args:
            payload: The request body for /v1/chat/completions.
            timeout: Per-request timeout in seconds.
            retries: Number of attempts (including the first).
            retry_backoff: Base backoff in seconds, doubled per attempt.

        Returns:
            requests.Response with .json() available. On adaptive acquire
            failure, returns a response whose .json() yields {"choices": []}
            so the caller treats it as a parse miss (empty inferences).

        Raises:
            requests.RequestException if the request ultimately fails.
        """
        acquired = False
        t0 = time.monotonic()
        is_error = False
        response = None
        try:
            if self._adaptive is not None:
                if not self._adaptive.acquire(timeout=timeout + 10):
                    self.logger.warning(
                        "Adaptive semaphore acquire timeout, returning empty"
                    )
                    return _EmptyLlmResponse()
                acquired = True

            response = None
            last_error = None
            for attempt in range(retries):
                try:
                    response = requests.post(
                        f"{LLM_URL}/v1/chat/completions",
                        json=payload,
                        timeout=timeout,
                    )
                    response.raise_for_status()
                    break
                except (requests.Timeout, requests.ConnectionError) as e:
                    last_error = e
                    if attempt < retries - 1:
                        wait = retry_backoff * (2 ** attempt)
                        self.logger.warning(
                            f"LLM call attempt {attempt + 1}/{retries} failed: {e}, "
                            f"retrying in {wait:.1f}s"
                        )
                        time.sleep(wait)
                    else:
                        self.logger.error(
                            f"LLM call failed after {retries} attempts: {e}"
                        )
                        raise
            if response is None:
                raise last_error
            return response
        except Exception:
            is_error = True
            raise
        finally:
            latency_ms = (time.monotonic() - t0) * 1000
            tokens_per_sec = self._tokens_per_sec_from(response, latency_ms, is_error)
            # Spec 1.3: expose LLM throughput even without the semaphore.
            self.llm_tokens_per_sec.set(tokens_per_sec)
            if acquired:
                self._adaptive.release(is_error=is_error)
                if self._llm_requests_counter is not None:
                    self._llm_requests_counter.inc()
                    if is_error:
                        self._llm_timeouts_counter.inc()
                # Live congestion-window metrics (not just at shutdown).
                self._export_adaptive_metrics()

    def _tokens_per_sec_from(
        self, response: Optional[requests.Response], latency_ms: float, is_error: bool
    ) -> float:
        if is_error or response is None or latency_ms <= 0:
            return 0.0
        try:
            usage = response.json().get("usage", {})
            completion_tokens = int(usage.get("completion_tokens", 0))
            if completion_tokens > 0:
                return completion_tokens / (latency_ms / 1000.0)
        except Exception:
            pass
        return 0.0

    def extract_inferences(
        self,
        chunk_text: str,
        entities: List[Dict[str, Any]],
        source_type: str,
    ) -> List[Dict[str, Any]]:
        """
        Extract micro-inferences (facts) from chunk text using an external LLM.

        This method builds a prompt from chunk text and detected entities, sends it to
        the vLLM /v1/chat/completions API, and parses the JSON response to extract
        structured facts.

        Input Processing:
            - Uses full chunk text (no truncation) for fact extraction
            - Entity names are NOT included in prompt (entities parameter is received for
              backward compatibility but not used in the LLM call)
            - Source type (notariado, catastro, etc.) is provided for document context

        LLM Prompt Structure:
            - System prompt: Instructs LLM to synthesize condensed facts (not copy text)
            - User prompt: Provides chunk text and dynamic_max inference count
            - Temperature: 0.1 (low randomness, deterministic)
            - Thinking disabled: enable_thinking=False to avoid <think> tags

        Response Processing:
            1. Cleans markdown code blocks: removes ```...``` wrappers
            2. Removes thinking tags: <think>...</think> blocks (some models output these)
            3. Extracts outermost JSON array using bracket counting
            4. Parses JSON and validates each item has "text" field
            5. Normalizes fields: text (string), confidence (float), entity_refs (list)
            6. Filters out inferences below MIN_CONFIDENCE_THRESHOLD (silent filter)

        vLLM API Contract:
            - Endpoint: POST {LLM_URL}/v1/chat/completions
            - Request: {"model": model_id, "messages": [...], "max_tokens": N, ...}
            - Response: {"choices": [{"message": {"content": "..."}}]}
            - Timeout: 30 seconds per request

        Args:
            chunk_text (str): The document chunk text to analyze. Trusted internal source
                             (notariado, catastro documents). Full text sent to LLM
                             (no truncation).
            entities (List[Dict[str, Any]]): Named entities detected in this chunk.
                                             Received for backward compatibility with
                                             RabbitMQ message schema but NOT used in
                                             the LLM prompt.
            source_type (str): Document source type (e.g., "notariado", "catastro", "bancario").
                               Used for context but not currently interpolated in prompt.
            max_inferences is no longer a parameter — the inference count is determined
            dynamically by word_count (len(chunk_text.split())):
                - < 200 words  → MAX_INFERENCES_SHORT
                - 200-499 words → MAX_INFERENCES_MEDIUM
                - >= 500 words  → MAX_INFERENCES_LONG

        Returns:
            List[Dict[str, Any]]: List of extracted inferences, each with structure:
                {
                    "text": str,              # The factual statement (synthesized, not copied)
                    "confidence": float,      # Confidence score 0.0-1.0
                    "entity_refs": List[str]  # Names of entities referenced in the fact
                }
            Only inferences with confidence >= MIN_CONFIDENCE_THRESHOLD are returned.
            Returns empty list [] if:
                - No LLM configured (LLM_URL empty or not set)
                - Model discovery failed and no LLM_MODEL fallback (llm_model_id is None)
                - LLM request fails (network error, timeout, HTTP error)
                - Response parsing fails (invalid JSON, missing fields)

        Error Handling:
            - Never raises exceptions
            - Logs WARNING for each failure case (network, parsing, etc.)
            - Logs ERROR for unexpected exceptions
            - Logs DEBUG for raw LLM response and extracted JSON (first 400-500 chars)
            - Logs INFO on successful parse with inference count

        Security Note:
            - chunk_text is interpolated directly into the LLM prompt
            - This is SAFE because all documents are from trusted internal sources
              (notariado/cadastral/banking documents)
            - If this worker is extended to accept untrusted external documents,
              implement prompt injection safeguards (e.g., text sanitization, truncation)

        Token Management:
            - max_tokens calculated dynamically: max(200, model_max_len - 900)
            - 900-token overhead estimate: system_prompt (150) + user_prompt (300) + margin (450)
            - Prevents exceeding model's maximum context length
            - Falls back to 4096 if max_model_len unknown
        """
        if not LLM_URL or not self.llm_model_id:
            self.logger.warning(
                "No LLM configured or model discovery failed, skipping inferences"
            )
            return []

        # Check cache before calling LLM
        cache_key = self._cache_key(chunk_text, source_type)
        cached = self._get_cached(cache_key)
        if cached is not None:
            validated_cached = self._validate_cached_inferences(cached)
            self.logger.info(f"Returning {len(validated_cached)} cached inferences (filtered from {len(cached)})")
            return validated_cached

        # Dynamic max inferences based on chunk size (word count as proxy for length)
        word_count = len(chunk_text.split())
        if word_count < 200:
            dynamic_max = MAX_INFERENCES_SHORT
        elif word_count < 500:
            dynamic_max = MAX_INFERENCES_MEDIUM
        else:
            dynamic_max = MAX_INFERENCES_LONG

        try:
            # KNOWN LIMITATION: chunk_text is interpolated directly into the prompt.
            # This is safe because all documents are from trusted internal sources
            # (notariado, catastro, etc). If accepting untrusted external documents,
            # implement prompt injection safeguards (e.g., text sanitization/truncation).
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

            # Calculate max_tokens dynamically from discovered max_model_len
            # Estimate: overhead = system_prompt (150) + user_prompt (300) + margin (450)
            max_tokens = max(200, (self.llm_max_model_len or 4096) - 900)

            payload = {
                "model": self.llm_model_id,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "max_tokens": max_tokens,
                "temperature": 0.1,
                "chat_template_kwargs": {"enable_thinking": False},
            }

            response = self._call_llm(payload, timeout=LLM_TIMEOUT)

            result = response.json()
            completion_text = (
                result.get("choices", [{}])[0].get("message", {}).get("content", "")
            )

            self.logger.debug(f"Raw LLM response (first 500 chars): {completion_text[:500]}")

            # Remove markdown code blocks if present
            completion_text = re.sub(r"```.*?\n", "", completion_text, flags=re.DOTALL)
            completion_text = re.sub(r"```", "", completion_text)
            # Remove thinking tags: complete blocks first, then any dangling closing tag
            completion_text = re.sub(
                r"<think>.*?</think>", "", completion_text, flags=re.DOTALL
            )
            completion_text = re.sub(
                r"</think>.*", "", completion_text, flags=re.DOTALL
            )

            # Extract the outermost JSON array (handles nested arrays correctly)
            json_str = self._extract_outermost_array(completion_text)
            if not json_str:
                response_preview = (
                    completion_text[:300].replace("\n", " ")
                    if completion_text
                    else "(empty)"
                )
                self.logger.warning(
                    f"No JSON array found in LLM response. Response preview: {response_preview}"
                )
                return []

            self.logger.debug(f"Extracted JSON array (first 400 chars): {json_str[:400]}")

            try:
                inferences = json.loads(json_str)
                self.logger.debug(
                    f"Successfully parsed {len(inferences)} inferences from LLM response"
                )
            except json.JSONDecodeError as e:
                response_preview = completion_text[:500].replace("\n", " ")
                self.logger.warning(
                    f"Failed to parse LLM response JSON: {e.msg}. Response: {response_preview}"
                )
                return []

            # Validate and annotate
            validated = []
            for inf in inferences:
                if isinstance(inf, dict) and "text" in inf:
                    entity_refs_value = inf.get("entity_refs")
                    if entity_refs_value is None:
                        # Fallback: old LLM response used "entities" key
                        entity_refs_value = inf.get("entities", [])
                        if entity_refs_value:
                            self.logger.debug(
                                "LLM response used deprecated 'entities' key; "
                                "mapped to 'entity_refs' via fallback"
                            )
                    validated.append(
                        {
                            "text": inf.get("text", ""),
                            "confidence": float(inf.get("confidence", 0.5)),
                            "entity_refs": entity_refs_value,
                        }
                    )

            validated = [inf for inf in validated if inf["confidence"] >= MIN_CONFIDENCE_THRESHOLD]

            # Cache raw inferences (before confidence filter) for reuse
            self._set_cached(cache_key, inferences if isinstance(inferences, list) else [])

            self.logger.info(f"Extracted {len(validated)} inferences from chunk")
            return validated

        except requests.RequestException as e:
            self.logger.warning(f"LLM call failed: {e}")
            return []
        except json.JSONDecodeError as e:
            self.logger.warning(f"Failed to parse LLM response JSON: {e}")
            return []
        except Exception as e:
            self.logger.error(f"Error extracting inferences: {e}")
            return []

    def extract_inferences_batch(
        self,
        chunks_data: List[Dict[str, Any]],
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        Extract inferences from multiple chunks in a single LLM call.

        Sends all chunks to the LLM at once with a batch prompt.
        Falls back to individual processing if the batch call fails.

        Args:
            chunks_data: List of dicts, each with chunk_id, text, source_type, entities

        Returns:
            Dict mapping chunk_id -> list of inference dicts
        """
        if not LLM_URL or not self.llm_model_id:
            return {c["chunk_id"]: [] for c in chunks_data}

        results: Dict[str, List[Dict[str, Any]]] = {
            c["chunk_id"]: [] for c in chunks_data
        }
        if not chunks_data:
            return results

        try:
            passages = []
            for chunk in chunks_data:
                word_count = len(chunk["text"].split())
                if word_count < 200:
                    max_facts = MAX_INFERENCES_SHORT
                elif word_count < 500:
                    max_facts = MAX_INFERENCES_MEDIUM
                else:
                    max_facts = MAX_INFERENCES_LONG
                passages.append({
                    "passage_id": str(chunk["chunk_id"]),
                    "text": chunk["text"],
                    "max_facts": max_facts,
                })

            passages_text = "\n\n---\n\n".join(
                f'PASSAGE {p["passage_id"]} (extract up to {p["max_facts"]} facts):\n{p["text"]}'
                for p in passages
            )

            system_prompt = """You are a precise fact-extraction engine. You will receive multiple text passages, each identified by a passage_id.

Rules:
- For EACH passage, extract the most important facts as SYNTHESIZED, CONDENSED statements.
- Each fact must be independently understandable without reading the original text.
- Mention specific values, names, dates, or amounts whenever they are in the text.
- Do NOT include vague or generic statements.
- Respond ONLY with a valid JSON array. No explanation. No text outside the JSON.

Each object in the array must have:
- "passage_id": the passage identifier (string)
- "facts": array of fact objects, each with "text", "confidence" (0.0-1.0), "entity_refs" (list of strings)"""

            user_prompt = f"""Extract facts from these {len(passages)} passages:

{passages_text}

Respond with ONLY the JSON array:"""

            max_tokens = max(500, (self.llm_max_model_len or 4096) - 1500)

            payload = {
                "model": self.llm_model_id,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "max_tokens": max_tokens,
                "temperature": 0.1,
                "chat_template_kwargs": {"enable_thinking": False},
            }

            batch_timeout = max(LLM_TIMEOUT, min(180, len(chunks_data) * LLM_TIMEOUT))

            response = self._call_llm(payload, timeout=batch_timeout)

            result = response.json()
            completion_text = (
                result.get("choices", [{}])[0].get("message", {}).get("content", "")
            )

            batch_results = self._parse_batch_response(
                completion_text, [c["chunk_id"] for c in chunks_data]
            )
            results.update(batch_results)
            return results

        except Exception as e:
            self.logger.error(f"Batch extraction failed ({len(chunks_data)} chunks): {e}")
            raise

    def _parse_batch_response(
        self, content: str, expected_chunk_ids: List
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        Parse batch LLM response and map results to individual chunk_ids.

        Returns dict mapping chunk_id -> list of validated inferences.
        """
        results: Dict[str, List[Dict[str, Any]]] = {
            str(cid): [] for cid in expected_chunk_ids
        }

        try:
            # Clean markdown code blocks
            content = re.sub(r"```.*?\n", "", content, flags=re.DOTALL)
            content = re.sub(r"```", "", content)

            # Try direct JSON parse first (most common case)
            data = None

            # Method 1: Direct parse of cleaned content
            try:
                data = json.loads(content.strip())
                if not isinstance(data, list):
                    data = None
            except json.JSONDecodeError:
                pass

            # Method 2: Extract JSON array using bracket counting
            if data is None:
                json_str = self._extract_outermost_array(content)
                if json_str:
                    try:
                        data = json.loads(json_str)
                    except json.JSONDecodeError:
                        pass

            if data is None:
                self.logger.warning("No JSON array found in batch response")
                return results

            for item in data:
                if not isinstance(item, dict):
                    continue
                passage_id = str(item.get("passage_id", ""))
                facts = item.get("facts", [])

                if passage_id not in results:
                    continue

                validated = []
                for fact in facts:
                    if isinstance(fact, dict) and "text" in fact:
                        conf = float(fact.get("confidence", 0.5))
                        entity_refs = fact.get("entity_refs", fact.get("entities", []))
                        if conf >= MIN_CONFIDENCE_THRESHOLD:
                            validated.append({
                                "text": fact["text"],
                                "confidence": conf,
                                "entity_refs": entity_refs if isinstance(entity_refs, list) else [],
                            })
                results[passage_id] = validated

            found = [cid for cid in results if results[cid]]
            self.logger.info(
                f"Batch parsed: {len(found)}/{len(expected_chunk_ids)} chunks have results"
            )
            return results

        except Exception as e:
            self.logger.error(f"Error parsing batch response: {e}")
            return results

    def process(self, ch, method, properties, body):
        """
        Process an inference job from RabbitMQ inferences queue.

        Adaptive mode (INFERENCE_ADAPTIVE_ENABLED): dispatch each message to
        the ThreadPoolExecutor and return immediately so pika keeps consuming;
        acks/nacks are scheduled back onto the pika thread via
        connection.add_callback_threadsafe.

        Legacy modes: when batch processing is enabled, messages are
        accumulated in a buffer and processed together when the buffer is full
        or a timeout expires. When batch processing is disabled, each message
        is processed individually.
        """
        if ADAPTIVE_ENABLED:
            if _stopping:
                # Shutting down: don't start new work; let the broker
                # redeliver this chunk after restart.
                self.jobs_total.labels(status="shutdown_rejected").inc()
                ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
                return
            self._dispatch_adaptive(body, method.delivery_tag)
            return

        if not BATCH_ENABLED:
            self._process_single(ch, method, properties, body)
            return

        # Batch mode: accumulate message in buffer
        try:
            message = json.loads(body)
            self._observe_queue_time(message)

            required_fields = ["job_id", "chunk_id", "chunk_text", "total_chunks"]
            missing = [f for f in required_fields if f not in message]
            if missing:
                self.logger.error(f"Missing required fields: {missing}")
                self.jobs_total.labels(status="invalid_message").inc()
                ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
                return

            chunk_text = message.get("chunk_text", "")
            if not chunk_text:
                self.jobs_total.labels(status="no_text").inc()
                ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
                return

            word_count = len(chunk_text.split())
            if word_count > MAX_CHUNK_WORDS:
                self.logger.warning(
                    f"Chunk too large: {word_count} words (max {MAX_CHUNK_WORDS}), "
                    f"job={message.get('job_id')}, chunk={message.get('chunk_id')}"
                )
                self.jobs_total.labels(status="chunk_too_large").inc()
                self._store_empty_result(ch, method, message)
                return

            batch_to_process = None
            with self._batch_lock:
                self._batch_buffer.append({
                    "ch": ch,
                    "method": method,
                    "body": body,
                    "message": message,
                })

                if len(self._batch_buffer) >= BATCH_SIZE:
                    batch_to_process = self._batch_buffer[:]
                    self._batch_buffer.clear()

            # Process batch OUTSIDE the lock to avoid blocking new messages
            if batch_to_process:
                self._process_batch(batch_to_process)
                return
        except Exception as e:
            self.logger.error(f"Error in batch accumulation: {e}")
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)

    def _dispatch_adaptive(self, body: bytes, delivery_tag: int) -> None:
        """Submit a per-chunk processing task to the executor."""
        with self._tasks_lock:
            self._tasks_in_flight += 1
        self._executor.submit(self._adaptive_task, body, delivery_tag)

    def _schedule_on_pika(self, conn, ch, callback) -> None:
        """Run a pika operation on the consumer thread (thread-safe).

        ``conn``/``ch`` must be the pair captured when the work was created,
        NOT the current worker refs: if the consumer reconnected meanwhile,
        scheduling on the new connection with a stale channel would tear the
        fresh epoch down. When the captured epoch is closed the broker has
        already requeued its unacked messages, so dropping is correct.
        """
        if conn is None or not conn.is_open or ch is None:
            self.logger.warning(
                "callback dropped: connection epoch closed (broker will redeliver)"
            )
            return
        try:
            conn.add_callback_threadsafe(callback)
        except Exception as e:
            self.logger.warning(f"add_callback_threadsafe failed: {e}")

    def _persist_chunk_result_once(
        self, job_id, chunk_id, chunk_result: Dict[str, Any]
    ) -> tuple:
        """Persist a chunk result exactly once across redeliveries.

        Returns ``(stored: bool, remaining: Optional[int])``. ``remaining`` is
        None when the chunk was already persisted on a previous delivery
        (redelivery after a dropped ack must not double-rpush/double-decr).
        """
        dedup_key = f"orchestrator:job:{job_id}:inferences:chunk_done:{chunk_id}"
        added = self.redis_client.set(dedup_key, "1", nx=True, ex=RAW_TTL_SECONDS)
        if not added:
            return (False, None)

        inferences_raw_key = f"orchestrator:job:{job_id}:micro_inferences_raw"
        self.redis_client.rpush(inferences_raw_key, json.dumps(chunk_result))
        self.redis_client.expire(inferences_raw_key, RAW_TTL_SECONDS)

        remaining_key = f"orchestrator:job:{job_id}:inferences:remaining"
        remaining = self.redis_client.decr(remaining_key)
        return (True, remaining)

    def _request_consumer_stop(self, conn, ch) -> None:
        """Ask the pika thread to stop consuming (last task on shutdown)."""
        self._schedule_on_pika(
            conn, ch, lambda: ch.stop_consuming() if ch is not None else None
        )

    def _adaptive_task(self, body: bytes, delivery_tag: int) -> None:
        """
        Process one chunk outside the pika thread.

        The channel is NEVER touched here directly: ack/nack are scheduled
        onto the consumer thread via add_callback_threadsafe, always against
        the connection/channel pair captured at task start (epoch binding) so
        a reconnect cannot route a stale-channel op onto a fresh connection.
        The AdaptiveSemaphore inside _call_llm gates LLM concurrency (cwnd),
        so up to ADAPTIVE_MAX_CONCURRENCY tasks may be running at once.
        """
        conn = self._connection
        ch = self._channel

        def ack():
            if ch is not None:
                self._schedule_on_pika(
                    conn, ch, lambda: ch.basic_ack(delivery_tag=delivery_tag)
                )

        def nack(requeue: bool):
            if ch is not None:
                self._schedule_on_pika(
                    conn,
                    ch,
                    lambda: ch.basic_nack(
                        delivery_tag=delivery_tag, requeue=requeue
                    ),
                )

        try:
            message = json.loads(body)
            self._observe_queue_time(message)

            job_id = message.get("job_id")
            chunk_id = message.get("chunk_id")
            total_chunks = message.get("total_chunks", 1)
            chunk_text = message.get("chunk_text", "")
            entities = message.get("entities", [])
            source_type = message.get("source_type", "generico")

            required_fields = ["job_id", "chunk_id", "chunk_text", "total_chunks"]
            missing = [f for f in required_fields if f not in message]
            if missing or not chunk_text:
                status = "invalid_message" if missing else "no_text"
                self.logger.error(f"{status} (adaptive): {missing or job_id}")
                self.jobs_total.labels(status=status).inc()
                nack(requeue=False)
                return

            word_count = len(chunk_text.split())
            if word_count > MAX_CHUNK_WORDS:
                self.logger.warning(
                    f"Chunk too large: {word_count} words (max {MAX_CHUNK_WORDS}), "
                    f"job={job_id}, chunk={chunk_id}"
                )
                self.jobs_total.labels(status="chunk_too_large").inc()
                self._store_empty_result_data(job_id, chunk_id, total_chunks)
                ack()
                return

            inferences = self.extract_inferences(
                chunk_text=chunk_text,
                entities=entities,
                source_type=source_type,
            )

            chunk_result = {"chunk_id": chunk_id, "inferences": inferences}
            stored, remaining = self._persist_chunk_result_once(
                job_id, chunk_id, chunk_result
            )
            if not stored:
                # Already processed on a previous delivery (dropped ack
                # triggered redelivery). Do not double-write/decrement.
                self.logger.info(
                    f"Duplicate delivery ignored job={job_id} chunk={chunk_id}"
                )
                self.jobs_total.labels(status="chunk_processed").inc()
                ack()
                return

            if remaining <= 0:
                self._assemble_final_results(job_id)
                self.jobs_total.labels(status="success").inc()
            else:
                self.event_bus.publish_job_inference_chunk_progress(
                    job_id,
                    chunks_done=total_chunks - remaining,
                    chunks_total=total_chunks,
                )
                self.jobs_total.labels(status="chunk_processed").inc()

            self.logger.info(
                f"Inference completed (adaptive) job={job_id} chunk={chunk_id} "
                f"inferences={len(inferences)} remaining={remaining}"
            )
            ack()

        except Exception as e:
            self.logger.error(f"Adaptive task failed: {e}")
            self.jobs_total.labels(status="error").inc()
            nack(requeue=True)
        finally:
            with self._tasks_lock:
                self._tasks_in_flight -= 1
                drained = self._tasks_in_flight == 0
            if drained and _stopping:
                self._request_consumer_stop(conn, ch)

    def flush_batch_buffer(self):
        """Flush any pending messages in the batch buffer. Called by pika call_later callback."""
        with self._batch_lock:
            if not self._batch_buffer:
                return
            batch = self._batch_buffer[:]
            self._batch_buffer.clear()

        if batch:
            self.logger.info(f"Timer flush: processing {len(batch)} buffered messages")
            try:
                self._process_batch(batch)
            except Exception as e:
                self.logger.error(f"Timer flush failed: {e}")
                for item in batch:
                    try:
                        item["ch"].basic_nack(
                            delivery_tag=item["method"].delivery_tag,
                            requeue=True
                        )
                    except Exception as nack_error:
                        self.logger.warning(f"Failed to NACK message: {nack_error}")

    def _store_empty_result_data(self, job_id, chunk_id, total_chunks) -> None:
        """Persist an empty result for a skipped/oversized chunk (no ack)."""
        chunk_result = {
            "chunk_id": chunk_id,
            "inferences": [],
        }

        stored, remaining = self._persist_chunk_result_once(
            job_id, chunk_id, chunk_result
        )
        if not stored:
            self.logger.info(
                f"Duplicate empty result ignored job={job_id} chunk={chunk_id}"
            )
            self.jobs_total.labels(status="chunk_processed").inc()
            return

        self.logger.info(
            f"Inference skipped for job: {job_id}, chunk: {chunk_id}, remaining: {remaining}"
        )

        if remaining <= 0:
            self._assemble_final_results(job_id)
            self.jobs_total.labels(status="success").inc()
        else:
            chunks_done = total_chunks - remaining
            self.event_bus.publish_job_inference_chunk_progress(
                job_id, chunks_done=chunks_done, chunks_total=total_chunks
            )
            self.jobs_total.labels(status="chunk_processed").inc()

    def _store_empty_result(self, ch, method, message):
        """Store empty inference result and ACK the message. Used for skipped/oversized chunks."""
        job_id = message.get("job_id")
        chunk_id = message.get("chunk_id")
        total_chunks = message.get("total_chunks", 1)

        self._store_empty_result_data(job_id, chunk_id, total_chunks)

        ch.basic_ack(delivery_tag=method.delivery_tag)

    def _process_batch(self, batch: List[Dict[str, Any]]):
        """
        Process a batch of accumulated messages using a single LLM call.

        For each chunk in the batch:
        1. Check cache (skip LLM if hit)
        2. Group uncached chunks for batch LLM call
        3. Fallback to individual processing if batch fails
        """
        start_time = time.time()
        self.batch_counter.labels(type="batch_start").inc()
        self.logger.info(f"Processing batch of {len(batch)} chunks")

        # Separate cached vs uncached
        cached_results = {}
        uncached_chunks = []
        uncached_indices = []

        for i, item in enumerate(batch):
            msg = item["message"]
            chunk_text = msg.get("chunk_text", "")
            source_type = msg.get("source_type", "generico")
            chunk_id = msg.get("chunk_id")

            cache_key = self._cache_key(chunk_text, source_type)
            cached = self._get_cached(cache_key)
            if cached is not None:
                validated = self._validate_cached_inferences(cached)
                cached_results[str(chunk_id)] = validated
                self.batch_counter.labels(type="cache_hit").inc()
            else:
                uncached_chunks.append({
                    "chunk_id": str(chunk_id),
                    "text": chunk_text,
                    "source_type": source_type,
                    "entities": msg.get("entities", []),
                })
                uncached_indices.append(i)

        # Process uncached chunks via batch LLM call
        if uncached_chunks:
            try:
                batch_results = self.extract_inferences_batch(uncached_chunks)
                for chunk in uncached_chunks:
                    cid = chunk["chunk_id"]
                    cache_key = self._cache_key(chunk["text"], chunk["source_type"])
                    raw = batch_results.get(cid, [])
                    self._set_cached(cache_key, raw)
                cached_results.update(batch_results)
                self.batch_counter.labels(type="batch_success").inc()
            except Exception as e:
                self.logger.warning(f"Batch LLM call failed, falling back to individual: {e}")
                self.batch_counter.labels(type="batch_fallback").inc()
                # Fallback: process each uncached chunk individually
                for chunk in uncached_chunks:
                    try:
                        inferences = self.extract_inferences(
                            chunk_text=chunk["text"],
                            entities=chunk.get("entities", []),
                            source_type=chunk.get("source_type", "generico"),
                        )
                        cached_results[chunk["chunk_id"]] = inferences
                    except Exception as individual_error:
                        self.logger.error(f"Individual fallback also failed: {individual_error}")
                        self.batch_counter.labels(type="individual_fallback_error").inc()
                        cached_results[chunk["chunk_id"]] = []

        # Save results for each chunk in batch
        for item in batch:
            msg = item["message"]
            ch = item["ch"]
            method = item["method"]
            job_id = msg.get("job_id")
            chunk_id = msg.get("chunk_id")
            total_chunks = msg.get("total_chunks", 1)

            inferences = cached_results.get(str(chunk_id), [])

            chunk_result = {
                "chunk_id": chunk_id,
                "inferences": inferences,
            }

            inferences_raw_key = f"orchestrator:job:{job_id}:micro_inferences_raw"
            self.redis_client.rpush(inferences_raw_key, json.dumps(chunk_result))
            self.redis_client.expire(inferences_raw_key, RAW_TTL_SECONDS)

            remaining_key = f"orchestrator:job:{job_id}:inferences:remaining"
            remaining = self.redis_client.decr(remaining_key)

            self.logger.info(
                f"Inference completed for job: {job_id}, chunk: {chunk_id}, "
                f"inferences: {len(inferences)}, remaining chunks: {remaining}"
            )

            if remaining <= 0:
                self._assemble_final_results(job_id)
                self.jobs_total.labels(status="success").inc()
            else:
                chunks_done = total_chunks - remaining
                self.event_bus.publish_job_inference_chunk_progress(
                    job_id, chunks_done=chunks_done, chunks_total=total_chunks
                )
                self.jobs_total.labels(status="chunk_processed").inc()

            ch.basic_ack(delivery_tag=method.delivery_tag)

        duration = time.time() - start_time
        self.job_duration.observe(duration)

    def _assemble_final_results(self, job_id: str):
        """Assemble final inference results when all chunks complete."""
        assembly_lock_key = f"orchestrator:job:{job_id}:inferences:assembly_lock"
        acquired = self.redis_client.setnx(assembly_lock_key, "1")
        self.redis_client.expire(assembly_lock_key, 3600)

        if not acquired:
            self.logger.warning(f"Assembly lock already held for job {job_id}, skipping")
            return

        try:
            inferences_raw_key = f"orchestrator:job:{job_id}:micro_inferences_raw"
            raw_results = self.redis_client.lrange(inferences_raw_key, 0, -1)

            assembled = []
            for raw_json in raw_results:
                try:
                    chunk_data = json.loads(raw_json)
                    assembled.append(chunk_data)
                except json.JSONDecodeError as e:
                    self.logger.warning(f"Failed to parse intermediate result: {e}")

            assembled.sort(key=lambda x: x.get("chunk_id") or 0)

            final_key = f"orchestrator:job:{job_id}:micro_inferences"
            self.redis_client.set(final_key, json.dumps(assembled))

            remaining_key = f"orchestrator:job:{job_id}:inferences:remaining"
            self.redis_client.delete(inferences_raw_key)
            self.redis_client.delete(remaining_key)

            self.redis_client.hset(
                f"orchestrator:job:{job_id}:steps", "inferences", "completed"
            )

            self.event_bus.publish_job_progress(job_id, 80, "inferences")

            self.logger.info(
                f"Inferences finalized for job: {job_id}, "
                f"total chunks: {len(assembled)}, "
                f"total inferences: {sum(len(c['inferences']) for c in assembled)}"
            )

        except Exception as e:
            self.logger.error(f"Error assembling final inferences: {e}")
            self.redis_client.hset(
                f"orchestrator:job:{job_id}:steps", "inferences", "failed"
            )
            self.jobs_total.labels(status="assembly_error").inc()

    def cleanup(self) -> None:
        """Flush pending batch and join timer thread on shutdown."""
        super().cleanup()
        self._shutdown_requested = True
        with self._batch_lock:
            if self._batch_buffer:
                if self._connection is not None and self._connection.is_open:
                    # Live channel (clean shutdown): the finally-block usually
                    # drained it; this is a safety net.
                    try:
                        self._process_batch(self._batch_buffer[:])
                    except Exception as e:
                        self.logger.error(f"Final batch flush failed: {e}")
                else:
                    # Dead channel (connection error at shutdown): do NOT
                    # process here. Clearing without acking lets the broker
                    # redeliver and process each message exactly once.
                    self.logger.warning(
                        f"Discarding {len(self._batch_buffer)} buffered batch "
                        "messages on dead channel (will be redelivered)"
                    )
                self._batch_buffer.clear()

        # Adaptive mode: wait for in-flight executor tasks (each drains its
        # own LLM call) before exiting. Do not sys.exit here — tests mock it.
        if self._adaptive is not None:
            wait_timeout = LLM_TIMEOUT + 30
            deadline = time.monotonic() + wait_timeout
            while self._tasks_in_flight > 0 and time.monotonic() < deadline:
                time.sleep(0.1)
            if self._tasks_in_flight > 0:
                self.logger.warning(
                    f"Force shutdown: {self._tasks_in_flight} tasks still in-flight"
                )
            elif self._adaptive.in_flight > 0:
                self.logger.warning(
                    f"Force shutdown: {self._adaptive.in_flight} LLM calls still in-flight"
                )
            else:
                self.logger.info(
                    "All in-flight LLM calls completed, shutting down"
                )
            self._export_adaptive_metrics()

    def _export_adaptive_metrics(self) -> None:
        """Publish adaptive concurrency gauges (called after each LLM call).

        Only gauges are set here. The requests/timeouts counters are
        incremented in _call_llm per event, so deriving them from the
        semaphore's cumulative stats here would double-count.
        """
        if self._adaptive is None or self._cwnd_gauge is None:
            return
        stats = self._adaptive.get_stats()
        self._cwnd_gauge.set(stats["cwnd"])
        self._in_flight_gauge.set(stats["in_flight"])
        self._cooldown_gauge.set(1 if stats["is_in_cooldown"] else 0)

    def _process_single(self, ch, method, properties, body):
        """
        Process an inference job from RabbitMQ inferences queue.

        This is the RabbitMQ message callback. Orchestrates the full inference workflow:
        extracts facts from a single chunk, stores intermediate results, and assembles
        final output when all chunks complete.

        Workflow:
            1. Parse message: job_id, chunk_id, chunk_text, entities, source_type, total_chunks
            2. Call extract_inferences() to get facts from this chunk
            3. Store result in Redis intermediate list (micro_inferences_raw)
            4. Decrement atomic counter (inferences:remaining)
            5. If counter reaches 0 (last chunk):
               a. Acquire assembly lock (SETNX) to prevent double-processing
               b. Retrieve all intermediate results from Redis
               c. Sort by chunk_id for deterministic ordering
               d. Store assembled result in Redis (micro_inferences key)
               e. Clean up intermediate keys and counter
               f. Mark step as "completed" in job steps hash
               g. Publish progress event (80% complete)
            6. If not last chunk:
               a. Calculate progress (chunks_done / total_chunks)
               b. Publish incremental progress event for UI feedback

        Message Format (Expected Input):
            {
                "job_id": str,          # Job identifier
                "chunk_id": int,        # Chunk sequence number
                "chunk_text": str,      # Document text to analyze
                "entities": [{...}],    # Named entities list
                "source_type": str,     # Document source (notariado, etc.)
                "total_chunks": int     # Total chunks for this job
            }

        Redis Keys Used:
            - orchestrator:job:{job_id}:micro_inferences_raw
              Intermediate list of per-chunk results (auto-expires 24h)
            - orchestrator:job:{job_id}:inferences:remaining
              Counter tracking chunks not yet processed (decremented per message)
            - orchestrator:job:{job_id}:inferences:assembly_lock
              SETNX lock to prevent double-assembly on message redelivery
            - orchestrator:job:{job_id}:micro_inferences
              Final assembled result (all chunks' inferences in order)
            - orchestrator:job:{job_id}:steps
              Job steps hash; this worker sets steps[inferences] = "completed"

        RabbitMQ Handling:
            - Auto-ack disabled (manual ack/nack)
            - On success: basic_ack() acknowledges message
            - On error: basic_nack(requeue=True) returns message to queue for retry
            - Covers network errors, processing errors, Redis errors

        Assembly Lock Behavior:
            - If RabbitMQ redelivers last chunk message, only first caller assembles
            - Lock is atomic (SETNX) and expires in 3600 seconds
            - Prevents data corruption from concurrent assembly attempts
            - If lock already held, logs warning and skips assembly

        Error Handling:
            - Empty chunk_text: nack and skip
            - JSON parse error: nack and requeue
            - Any other error: nack and requeue
            - Extract_inferences failure: captured gracefully, empty inference list handled
            - Assembly failure: sets step status to "failed", nacks message

        Progress Reporting:
            - On last chunk completion: publishes job_progress event (80%)
            - On intermediate chunks: publishes chunk_progress event with done/total counts
            - Helps UI show real-time progress to user

        Metrics:
            - Increments jobs_total counter with status label
            - Records job_duration histogram (per chunk)

        Args:
            ch: RabbitMQ channel object
            method: RabbitMQ method frame (contains delivery_tag)
            properties: RabbitMQ message properties
            body (bytes): JSON-encoded message body

        Side Effects:
            - Reads/writes Redis keys
            - Publishes events to Event Bus
            - Logs INFO/WARNING/ERROR messages
            - Records Prometheus metrics
        """
        start_time = time.time()
        job_id = None
        chunk_id = None

        try:
            message = json.loads(body)
            self._observe_queue_time(message)
            job_id = message.get("job_id")
            chunk_id = message.get("chunk_id")
            chunk_text = message.get("chunk_text", "")
            entities = message.get("entities", [])
            source_type = message.get("source_type", "generico")
            total_chunks = message.get("total_chunks", 1)

            self.logger.info(f"Processing inferences for job: {job_id}, chunk: {chunk_id}")

            if not chunk_text:
                self.logger.warning(
                    f"No text in message for job: {job_id}, chunk: {chunk_id}"
                )
                self.jobs_total.labels(status="no_text").inc()
                ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
                return

            word_count = len(chunk_text.split())
            if word_count > MAX_CHUNK_WORDS:
                self.logger.warning(
                    f"Chunk too large: {word_count} words (max {MAX_CHUNK_WORDS}), "
                    f"job={job_id}, chunk={chunk_id}"
                )
                self.jobs_total.labels(status="chunk_too_large").inc()
                self._store_empty_result(ch, method, message)
                return

            # Extract inferences with entity context (no truncation)
            inferences = self.extract_inferences(
                chunk_text=chunk_text, entities=entities, source_type=source_type
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
            self.redis_client.expire(inferences_raw_key, RAW_TTL_SECONDS)

            # Decrement atomic counter
            remaining_key = f"orchestrator:job:{job_id}:inferences:remaining"
            remaining = self.redis_client.decr(remaining_key)

            self.logger.info(
                f"Inference completed for job: {job_id}, chunk: {chunk_id}, "
                f"inferences: {len(inferences)}, remaining chunks: {remaining}"
            )

            # If this is the last chunk (remaining <= 0), assemble final result
            if remaining <= 0:
                self.logger.info(
                    f"All inferences complete for job {job_id}, assembling results..."
                )

                # Assembly lock: prevents double-assembly on RabbitMQ message redelivery.
                # SETNX is atomic — only the first caller acquires the lock.
                assembly_lock_key = (
                    f"orchestrator:job:{job_id}:inferences:assembly_lock"
                )
                acquired = self.redis_client.setnx(assembly_lock_key, "1")
                self.redis_client.expire(assembly_lock_key, 3600)

                if not acquired:
                    self.logger.warning(
                        f"Assembly lock already held for job {job_id}, skipping duplicate assembly"
                    )
                    self.jobs_total.labels(status="chunk_processed").inc()
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
                            self.logger.warning(f"Failed to parse intermediate result: {e}")
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

                    self.logger.info(
                        f"Inferences finalized for job: {job_id}, "
                        f"total chunks: {len(assembled)}, "
                        f"total inferences: {sum(len(c['inferences']) for c in assembled)}"
                    )

                    self.jobs_total.labels(status="success").inc()

                except Exception as e:
                    self.logger.error(f"Error assembling final inferences: {e}")
                    # Mark as failed
                    self.redis_client.hset(
                        f"orchestrator:job:{job_id}:steps", "inferences", "failed"
                    )
                    self.jobs_total.labels(status="assembly_error").inc()
            else:
                # Not the last chunk — publish incremental progress so clients see activity
                chunks_done = total_chunks - remaining
                # remaining was already decremented; chunks_done = total - remaining
                self.event_bus.publish_job_inference_chunk_progress(
                    job_id,
                    chunks_done=chunks_done,
                    chunks_total=total_chunks,
                )
                self.jobs_total.labels(status="chunk_processed").inc()

            duration = time.time() - start_time
            self.job_duration.observe(duration)

            ch.basic_ack(delivery_tag=method.delivery_tag)

        except Exception as e:
            self.logger.error(f"Error processing inferences: {e}")
            self.jobs_total.labels(status="error").inc()
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)

        finally:
            if _stopping and ch.is_open:
                self.logger.info("Graceful shutdown: stopping consumer after current message")
                ch.stop_consuming()


def signal_handler(signum, frame):
    """Handle graceful shutdown signals."""
    global _stopping
    _stopping = True


def main():
    import signal

    global _stopping
    _stopping = False

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    worker = InferenceWorker()

    # BaseWorker.run() normally starts these; this worker uses its own pika
    # loop, so the metrics/health servers must be started explicitly.
    worker.start_servers()

    if BATCH_ENABLED:
        worker.logger.info(
            f"Batch mode ENABLED: batch_size={BATCH_SIZE}, "
            f"timeout={BATCH_TIMEOUT_MS}ms, cache={'ON' if CACHE_ENABLED else 'OFF'}"
        )
    else:
        worker.logger.info(f"Batch mode DISABLED, cache={'ON' if CACHE_ENABLED else 'OFF'}")

    while not _stopping:
        connection = None
        try:
            params = parse_rabbitmq_url(worker.rabbitmq_url)
            connection = pika.BlockingConnection(params)
            channel = connection.channel()
            # Live refs for the adaptive executor threads. They only use them
            # via connection.add_callback_threadsafe, never directly.
            worker._connection = connection
            worker._channel = channel

            if BATCH_ENABLED:
                with worker._batch_lock:
                    worker._batch_buffer.clear()

            worker.logger.info(f"Consuming from queue: {QUEUE_NAME}")

            from pkg.worker_common.rabbitmq import declare_queue
            declare_queue(channel, QUEUE_NAME)

            if ADAPTIVE_ENABLED:
                # One chunk per task; keep the window plus headroom queued so
                # tasks never starve. Respects an explicit PREFETCH_COUNT.
                adaptive_prefetch = ADAPTIVE_MAX_CONCURRENCY + BATCH_SIZE
                prefetch_count = int(os.getenv("PREFETCH_COUNT", str(adaptive_prefetch)))
            elif BATCH_ENABLED:
                prefetch_count = int(os.getenv("PREFETCH_COUNT", str(BATCH_SIZE * 2)))
            else:
                prefetch_count = int(os.getenv("PREFETCH_COUNT", "3"))
            channel.basic_qos(prefetch_count=prefetch_count)

            if ADAPTIVE_ENABLED:
                # Idle shutdown watchdog: with the executor draining work
                # asynchronously, an idle worker never reaches start_consuming
                # return on its own; poll so SIGTERM can stop it even when
                # there are no tasks to trigger the drained-stop.
                def _shutdown_watchdog():
                    if _stopping:
                        try:
                            channel.stop_consuming()
                        except Exception:
                            pass
                        return
                    try:
                        connection.call_later(1.0, _shutdown_watchdog)
                    except Exception:
                        pass

                connection.call_later(1.0, _shutdown_watchdog)

            if BATCH_ENABLED:
                def _schedule_flush():
                    if _stopping:
                        # Graceful shutdown: leave start_consuming() with a
                        # LIVE channel so the finally-block can flush the
                        # buffer and ack on it.
                        try:
                            channel.stop_consuming()
                        except Exception:
                            pass
                        return
                    try:
                        worker.flush_batch_buffer()
                    except Exception as e:
                        worker.logger.error(f"Error in flush callback: {e}")
                    if not _stopping:
                        try:
                            connection.call_later(
                                BATCH_TIMEOUT_MS / 1000.0, _schedule_flush
                            )
                        except Exception:
                            pass

                connection.call_later(BATCH_TIMEOUT_MS / 1000.0, _schedule_flush)

            channel.basic_consume(
                queue=QUEUE_NAME, on_message_callback=worker.process, auto_ack=False
            )

            channel.start_consuming()

        except Exception as e:
            worker.logger.error(f"RabbitMQ connection error: {e}")
            if not _stopping:
                time.sleep(5)
        finally:
            # Flush buffered batch messages ONLY on a clean exit (channel
            # live): a graceful shutdown leaves start_consuming via the
            # timer's stop_consuming, so acks here succeed. On a connection
            # error the channel is dead — do NOT process the buffer here;
            # the next connect clears it and the broker redelivers the
            # unacked messages, which are then processed exactly once.
            if BATCH_ENABLED and connection is not None and connection.is_open:
                try:
                    worker.flush_batch_buffer()
                except Exception as e:
                    worker.logger.error(f"Final batch flush failed: {e}")

            # Drop refs so executor threads stop scheduling new pika calls.
            worker._channel = None
            worker._connection = None
            if connection is not None and connection.is_open:
                try:
                    connection.close()
                except Exception:
                    pass

    # Flush the batch buffer and drain in-flight adaptive work exactly once.
    # (BaseWorker's own signal handler is not installed by this main().)
    worker.cleanup()

    worker.logger.info("Inference worker shutdown complete")


if __name__ == "__main__":
    main()
