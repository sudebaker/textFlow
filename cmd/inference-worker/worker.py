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
import logging
import time
import re
import hashlib
import threading
import redis
import pika
import requests
from typing import Dict, List, Any, Optional
from prometheus_client import Counter, Histogram, start_http_server

sys.path.insert(0, "/app")
from pkg.worker_common.rabbitmq import (
    parse_rabbitmq_url,
    connect_rabbitmq,
    declare_queue,
)
from pkg.events_python import EventBus

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

_stopping = False

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://rabbitmq:5672/")
QUEUE_NAME = os.getenv("QUEUE_NAME", "inferences")
METRICS_PORT = int(os.getenv("METRICS_PORT", "8006"))
LLM_URL = os.getenv("LLM_URL", "")  # Base URL without /v1 path
LLM_MODEL = os.getenv("LLM_MODEL", "")  # Will be auto-discovered, left empty by default
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

# Prometheus metrics
jobs_total = Counter("inference_worker_jobs_total", "Total jobs processed", ["status"])
job_duration = Histogram("inference_worker_job_duration_seconds", "Job duration")
batch_counter = Counter("inference_worker_batch_total", "Batch operations", ["type"])


class InferenceWorker:
    """
    RabbitMQ consumer that extracts micro-inferences from document chunks using an LLM.

    This worker subscribes to the inferences queue and processes chunks from extraction.
    For each chunk, it calls an external vLLM API to extract factual statements guided
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
        """
        Initialize the inference worker with Redis connection and model discovery.

        Behavior:
            1. Connects to Redis using REDIS_URL
            2. Creates Event Bus for publishing progress
            3. If LLM_URL is configured:
               a. Attempts to discover available models from vLLM /v1/models API
               b. If discovery succeeds, caches model_id and max_model_len
               c. If discovery fails but LLM_MODEL env var set, falls back to that value
               d. If both fail, sets llm_model_id to None (inferences permanently disabled)
            4. If LLM_URL is not configured, disables inferences entirely

        State Invariants:
            - self.llm_model_id is set once and never changes (no re-discovery)
            - self.llm_max_model_len may be None (use default 4096 as fallback)
            - If llm_model_id is None, extract_inferences() always returns []

        Side Effects:
            - Logs INFO message if model discovered
            - Logs INFO message if fallback to LLM_MODEL triggered
            - Logs WARNING message if model discovery fails (only if LLM_URL set)
        """
        self.redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        self.event_bus = EventBus(self.redis_client)

        # Batch processing buffer
        self._batch_buffer: List[Dict[str, Any]] = []
        self._batch_lock = threading.Lock()

        # Discover model at startup (once, cached)
        if LLM_URL:
            self.llm_model_id, self.llm_max_model_len = self._discover_model(LLM_URL)
            # Fallback to statically configured LLM_MODEL if discovery fails
            if not self.llm_model_id and LLM_MODEL:
                logger.info(
                    f"Model discovery failed, falling back to LLM_MODEL env var: {LLM_MODEL}"
                )
                self.llm_model_id = LLM_MODEL
                self.llm_max_model_len = None
        else:
            self.llm_model_id = None
            self.llm_max_model_len = None

    @staticmethod
    def _discover_model(llm_url: str) -> tuple[Optional[str], Optional[int]]:
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
            logger.warning("No LLM_URL configured, model discovery skipped")
            return (None, None)

        try:
            response = requests.get(
                f"{llm_url}/v1/models",
                timeout=5,
            )
            response.raise_for_status()
            models = response.json()

            if not models.get("data"):
                logger.warning(f"No models found in vLLM response: {models}")
                return (None, None)

            model_info = models["data"][0]
            model_id = model_info.get("id")
            max_model_len = model_info.get("max_model_len", 4096)

            logger.info(
                f"Discovered model '{model_id}' with max_model_len={max_model_len}"
            )
            return (model_id, max_model_len)

        except requests.RequestException as e:
            logger.warning(f"Failed to discover models from {llm_url}: {e}")
            return (None, None)
        except (KeyError, ValueError) as e:
            logger.warning(f"Failed to parse vLLM models response: {e}")
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
                logger.info(f"Cache HIT for {cache_key[:50]}")
                return json.loads(cached)
        except Exception as e:
            logger.warning(f"Cache read error: {e}")
        return None

    def _set_cached(self, cache_key: str, inferences: List[Dict[str, Any]]) -> None:
        """Store inferences in Redis cache with TTL."""
        if not CACHE_ENABLED:
            return
        try:
            self.redis_client.setex(cache_key, CACHE_TTL_SECONDS, json.dumps(inferences))
            logger.debug(f"Cached {len(inferences)} inferences (TTL: {CACHE_TTL_SECONDS}s)")
        except Exception as e:
            logger.warning(f"Cache write error: {e}")

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
            logger.warning(
                "No LLM configured or model discovery failed, skipping inferences"
            )
            return []

        # Check cache before calling LLM
        cache_key = self._cache_key(chunk_text, source_type)
        cached = self._get_cached(cache_key)
        if cached is not None:
            validated_cached = self._validate_cached_inferences(cached)
            logger.info(f"Returning {len(validated_cached)} cached inferences (filtered from {len(cached)})")
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
            # Cap at 4096 to avoid exceeding available context (vLLM enforces prompt+output <= max_model_len)
            max_tokens = max(200, min((self.llm_max_model_len or 4096) - 900, 4096))

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

            response = None
            last_error = None
            for attempt in range(LLM_RETRIES):
                try:
                    response = requests.post(
                        f"{LLM_URL}/v1/chat/completions",
                        json=payload,
                        timeout=LLM_TIMEOUT,
                    )
                    response.raise_for_status()
                    break
                except (requests.Timeout, requests.ConnectionError) as e:
                    last_error = e
                    if attempt < LLM_RETRIES - 1:
                        wait = LLM_RETRY_BACKOFF * (2 ** attempt)
                        logger.warning(
                            f"LLM call attempt {attempt + 1}/{LLM_RETRIES} failed: {e}, "
                            f"retrying in {wait:.1f}s"
                        )
                        time.sleep(wait)
                    else:
                        logger.error(
                            f"LLM call failed after {LLM_RETRIES} attempts: {e}"
                        )
                        raise
            if response is None:
                raise last_error

            result = response.json()
            completion_text = (
                result.get("choices", [{}])[0].get("message", {}).get("content", "")
            )

            logger.debug(f"Raw LLM response (first 500 chars): {completion_text[:500]}")

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
                logger.warning(
                    f"No JSON array found in LLM response. Response preview: {response_preview}"
                )
                return []

            logger.debug(f"Extracted JSON array (first 400 chars): {json_str[:400]}")

            try:
                inferences = json.loads(json_str)
                logger.debug(
                    f"Successfully parsed {len(inferences)} inferences from LLM response"
                )
            except json.JSONDecodeError as e:
                response_preview = completion_text[:500].replace("\n", " ")
                logger.warning(
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
                            logger.debug(
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

            # Cap per-chunk output at 800 tokens, total at 8192,
            # and never exceed available context (vLLM enforces prompt+output <= max_model_len)
            max_tokens = max(500, min(len(chunks_data) * 800, (self.llm_max_model_len or 4096) - 2000, 8192))

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

            response = None
            last_error = None
            for attempt in range(LLM_RETRIES):
                try:
                    response = requests.post(
                        f"{LLM_URL}/v1/chat/completions",
                        json=payload,
                        timeout=batch_timeout,
                    )
                    response.raise_for_status()
                    break
                except (requests.Timeout, requests.ConnectionError) as e:
                    last_error = e
                    if attempt < LLM_RETRIES - 1:
                        wait = LLM_RETRY_BACKOFF * (2 ** attempt)
                        logger.warning(
                            f"Batch LLM attempt {attempt + 1}/{LLM_RETRIES} failed: {e}, "
                            f"retrying in {wait:.1f}s"
                        )
                        time.sleep(wait)
                    else:
                        logger.error(
                            f"Batch LLM failed after {LLM_RETRIES} attempts: {e}"
                        )
                        raise
            if response is None:
                raise last_error

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
            logger.error(f"Batch extraction failed ({len(chunks_data)} chunks): {e}")
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
                logger.warning("No JSON array found in batch response")
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
            logger.info(
                f"Batch parsed: {len(found)}/{len(expected_chunk_ids)} chunks have results"
            )
            return results

        except Exception as e:
            logger.error(f"Error parsing batch response: {e}")
            return results

    def process(self, ch, method, properties, body):
        """
        Process an inference job from RabbitMQ inferences queue.

        When batch processing is enabled, messages are accumulated in a buffer
        and processed together when the buffer is full or a timeout expires.
        When batch processing is disabled, each message is processed individually.
        """
        if not BATCH_ENABLED:
            self._process_single(ch, method, properties, body)
            return

        # Batch mode: accumulate message in buffer
        try:
            message = json.loads(body)

            required_fields = ["job_id", "chunk_id", "chunk_text", "total_chunks"]
            missing = [f for f in required_fields if f not in message]
            if missing:
                logger.error(f"Missing required fields: {missing}")
                jobs_total.labels(status="invalid_message").inc()
                ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
                return

            chunk_text = message.get("chunk_text", "")
            if not chunk_text:
                jobs_total.labels(status="no_text").inc()
                ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
                return

            word_count = len(chunk_text.split())
            if word_count > MAX_CHUNK_WORDS:
                logger.warning(
                    f"Chunk too large: {word_count} words (max {MAX_CHUNK_WORDS}), "
                    f"job={message.get('job_id')}, chunk={message.get('chunk_id')}"
                )
                jobs_total.labels(status="chunk_too_large").inc()
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
            logger.error(f"Error in batch accumulation: {e}")
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)

    def flush_batch_buffer(self):
        """Flush any pending messages in the batch buffer. Called by pika call_later callback."""
        with self._batch_lock:
            if not self._batch_buffer:
                return
            batch = self._batch_buffer[:]
            self._batch_buffer.clear()

        if batch:
            logger.info(f"Timer flush: processing {len(batch)} buffered messages")
            try:
                self._process_batch(batch)
            except Exception as e:
                logger.error(f"Timer flush failed: {e}")
                for item in batch:
                    try:
                        item["ch"].basic_nack(
                            delivery_tag=item["method"].delivery_tag,
                            requeue=True
                        )
                    except Exception as nack_error:
                        logger.warning(f"Failed to NACK message: {nack_error}")

    def _store_empty_result(self, ch, method, message):
        """Store empty inference result and ACK the message. Used for skipped/oversized chunks."""
        job_id = message.get("job_id")
        chunk_id = message.get("chunk_id")
        total_chunks = message.get("total_chunks", 1)

        chunk_result = {
            "chunk_id": chunk_id,
            "inferences": [],
        }

        inferences_raw_key = f"orchestrator:job:{job_id}:micro_inferences_raw"
        self.redis_client.rpush(inferences_raw_key, json.dumps(chunk_result))
        self.redis_client.expire(inferences_raw_key, RAW_TTL_SECONDS)

        remaining_key = f"orchestrator:job:{job_id}:inferences:remaining"
        remaining = self.redis_client.decr(remaining_key)

        logger.info(
            f"Inference skipped for job: {job_id}, chunk: {chunk_id}, remaining: {remaining}"
        )

        if remaining <= 0:
            self._assemble_final_results(job_id)
            jobs_total.labels(status="success").inc()
        else:
            chunks_done = total_chunks - remaining
            self.event_bus.publish_job_inference_chunk_progress(
                job_id, chunks_done=chunks_done, chunks_total=total_chunks
            )
            jobs_total.labels(status="chunk_processed").inc()

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
        batch_counter.labels(type="batch_start").inc()
        logger.info(f"Processing batch of {len(batch)} chunks")

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
                batch_counter.labels(type="cache_hit").inc()
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
                batch_counter.labels(type="batch_success").inc()
            except Exception as e:
                logger.warning(f"Batch LLM call failed, falling back to individual: {e}")
                batch_counter.labels(type="batch_fallback").inc()
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
                        logger.error(f"Individual fallback also failed: {individual_error}")
                        batch_counter.labels(type="individual_fallback_error").inc()
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

            logger.info(
                f"Inference completed for job: {job_id}, chunk: {chunk_id}, "
                f"inferences: {len(inferences)}, remaining chunks: {remaining}"
            )

            if remaining <= 0:
                self._assemble_final_results(job_id)
                jobs_total.labels(status="success").inc()
            else:
                chunks_done = total_chunks - remaining
                self.event_bus.publish_job_inference_chunk_progress(
                    job_id, chunks_done=chunks_done, chunks_total=total_chunks
                )
                jobs_total.labels(status="chunk_processed").inc()

            ch.basic_ack(delivery_tag=method.delivery_tag)

        duration = time.time() - start_time
        job_duration.observe(duration)

    def _assemble_final_results(self, job_id: str):
        """Assemble final inference results when all chunks complete."""
        assembly_lock_key = f"orchestrator:job:{job_id}:inferences:assembly_lock"
        acquired = self.redis_client.setnx(assembly_lock_key, "1")
        self.redis_client.expire(assembly_lock_key, 3600)

        if not acquired:
            logger.warning(f"Assembly lock already held for job {job_id}, skipping")
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
                    logger.warning(f"Failed to parse intermediate result: {e}")

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

            logger.info(
                f"Inferences finalized for job: {job_id}, "
                f"total chunks: {len(assembled)}, "
                f"total inferences: {sum(len(c['inferences']) for c in assembled)}"
            )

        except Exception as e:
            logger.error(f"Error assembling final inferences: {e}")
            self.redis_client.hset(
                f"orchestrator:job:{job_id}:steps", "inferences", "failed"
            )
            jobs_total.labels(status="assembly_error").inc()

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
            job_id = message.get("job_id")
            chunk_id = message.get("chunk_id")
            chunk_text = message.get("chunk_text", "")
            entities = message.get("entities", [])
            source_type = message.get("source_type", "generico")
            total_chunks = message.get("total_chunks", 1)

            logger.info(f"Processing inferences for job: {job_id}, chunk: {chunk_id}")

            if not chunk_text:
                logger.warning(
                    f"No text in message for job: {job_id}, chunk: {chunk_id}"
                )
                jobs_total.labels(status="no_text").inc()
                ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
                return

            word_count = len(chunk_text.split())
            if word_count > MAX_CHUNK_WORDS:
                logger.warning(
                    f"Chunk too large: {word_count} words (max {MAX_CHUNK_WORDS}), "
                    f"job={job_id}, chunk={chunk_id}"
                )
                jobs_total.labels(status="chunk_too_large").inc()
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

            logger.info(
                f"Inference completed for job: {job_id}, chunk: {chunk_id}, "
                f"inferences: {len(inferences)}, remaining chunks: {remaining}"
            )

            # If this is the last chunk (remaining <= 0), assemble final result
            if remaining <= 0:
                logger.info(
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

        finally:
            if _stopping and ch.is_open:
                logger.info("Graceful shutdown: stopping consumer after current message")
                ch.stop_consuming()


def signal_handler(signum, frame):
    """
    Handle graceful shutdown on SIGINT or SIGTERM.

    Sets the global _stopping flag to stop the consumer loop after the current
    message finishes processing. Does NOT call sys.exit() to avoid interrupting
    a message in flight and leaving jobs in 'processing' state permanently.

    Args:
        signum (int): Signal number (signal.SIGINT or signal.SIGTERM)
        frame: Signal frame object (unused)
    """
    logger.info("Received shutdown signal, initiating graceful shutdown...")
    global _stopping
    _stopping = True


def main():
    """
    Entry point for the Inference Worker.

    Orchestrates:
        1. Signal registration: SIGINT/SIGTERM → graceful shutdown
        2. Prometheus metrics server startup on METRICS_PORT
        3. InferenceWorker initialization (Redis, model discovery)
        4. RabbitMQ consumer loop (infinite with reconnection)

    Workflow:
        1. Log startup message
        2. Register SIGINT/SIGTERM handlers for graceful shutdown
        3. Start Prometheus HTTP server on METRICS_PORT (default: 8006)
        4. Create InferenceWorker instance (triggers model discovery)
        5. Infinite loop:
           a. Connect to RabbitMQ
           b. Declare inferences queue
           c. Set prefetch_count for backpressure (default: 3)
           d. Register worker.process as message callback
           e. Start consuming messages
           f. On connection error: log error, sleep 5s, retry

    RabbitMQ Configuration:
        - Queue name: QUEUE_NAME (default: "inferences")
        - Prefetch count: PREFETCH_COUNT env var (default: 3)
          - Limits how many messages consumer holds unacked
          - Prevents overwhelming worker if backed up
        - Auto-ack: False (manual ack/nack in process callback)
        - Connection reconnect: Automatic with 5s retry interval

    Model Discovery:
        - Triggered in InferenceWorker.__init__() (one-time)
        - vLLM /v1/models endpoint queried if LLM_URL configured
        - Result logged (model_id and max_model_len discovered)
        - Fallback to LLM_MODEL env var if discovery fails
        - If both fail, inferences disabled (all extract_inferences calls return [])

    Prometheus Metrics:
        - Endpoint: http://localhost:{METRICS_PORT}/metrics
        - Metrics exposed:
          - inference_worker_jobs_total: Counter by status (success, error, assembly_error, etc.)
          - inference_worker_job_duration_seconds: Histogram of processing time per chunk

    Shutdown Sequence:
        1. SIGINT/SIGTERM → signal_handler() → sets _stopping=True → consumer finishes current message → stop_consuming()
        2. RabbitMQ channel cleanup (handled by context manager)
        3. Unack messages returned to queue for reprocessing

    Error Recovery:
        - RabbitMQ connection errors: log, sleep 5s, retry in main loop
        - Ensures worker is resilient to temporary network issues
        - No exponential backoff (always 5s retry interval)

    Dependencies (Environment Variables):
        - REDIS_URL: Redis connection (default: redis://redis:6379)
        - RABBITMQ_URL: RabbitMQ connection (default: amqp://rabbitmq:5672/)
        - QUEUE_NAME: Queue name (default: inferences)
        - LLM_URL: vLLM base URL (e.g., http://localhost:8000, may be empty)
        - LLM_MODEL: Fallback model ID (optional, used if discovery fails)
        - METRICS_PORT: Prometheus port (default: 8006)
        - PREFETCH_COUNT: RabbitMQ prefetch (default: 3)
    """
    import signal

    logger.info("Starting Inference Worker")

    global _stopping
    _stopping = False

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    start_http_server(METRICS_PORT)
    logger.info(f"Metrics server started on port {METRICS_PORT}")

    worker = InferenceWorker()

    if BATCH_ENABLED:
        logger.info(
            f"Batch mode ENABLED: batch_size={BATCH_SIZE}, "
            f"timeout={BATCH_TIMEOUT_MS}ms, cache={'ON' if CACHE_ENABLED else 'OFF'}"
        )
    else:
        logger.info(f"Batch mode DISABLED, cache={'ON' if CACHE_ENABLED else 'OFF'}")

    while not _stopping:
        try:
            with connect_rabbitmq(RABBITMQ_URL) as (connection, channel):
                # Clear batch buffer on reconnect — stale channel/delivery_tag refs
                # are invalid after a new connection. Unacked messages are requeued
                # by RabbitMQ automatically when the old connection drops.
                if BATCH_ENABLED:
                    with worker._batch_lock:
                        worker._batch_buffer.clear()

                logger.info(f"Consuming from queue: {QUEUE_NAME}")

                declare_queue(channel, QUEUE_NAME)

                if BATCH_ENABLED:
                    prefetch_count = int(os.getenv("PREFETCH_COUNT", str(BATCH_SIZE * 2)))
                else:
                    prefetch_count = int(os.getenv("PREFETCH_COUNT", "3"))
                channel.basic_qos(prefetch_count=prefetch_count)
                logger.info(f"Set prefetch_count to {prefetch_count}")

                # Schedule recurring flush timer using pika's call_later.
                # Runs on the same thread as message callbacks — no race conditions
                # with channel operations. Reschedules itself on each tick.
                # NOTE: Timer drift is possible — the next flush is scheduled AFTER
                # the current one completes. In practice, with batch_size=5 and
                # typical LLM latency of 5-10s, drift is negligible (<500ms).
                # For precise interval timing, use absolute timestamps.
                if BATCH_ENABLED:
                    def _schedule_flush():
                        if _stopping:
                            return
                        try:
                            worker.flush_batch_buffer()
                        except Exception as e:
                            logger.error(f"Error in flush callback: {e}")
                        if not _stopping:
                            try:
                                connection.call_later(
                                    BATCH_TIMEOUT_MS / 1000.0, _schedule_flush
                                )
                            except Exception:
                                pass  # Connection may be closing

                    connection.call_later(BATCH_TIMEOUT_MS / 1000.0, _schedule_flush)

                channel.basic_consume(
                    queue=QUEUE_NAME, on_message_callback=worker.process, auto_ack=False
                )

                channel.start_consuming()

        except Exception as e:
            logger.error(f"RabbitMQ connection error: {e}")
            if not _stopping:
                time.sleep(5)

    logger.info("Inference worker shutdown complete")


if __name__ == "__main__":
    main()
