#!/usr/bin/env python3
"""
Inference Worker for IA Text Orchestrator.

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

# Prometheus metrics
jobs_total = Counter("inference_worker_jobs_total", "Total jobs processed", ["status"])
job_duration = Histogram("inference_worker_job_duration_seconds", "Job duration")


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

            response = requests.post(
                f"{LLM_URL}/v1/chat/completions",
                json=payload,
                timeout=30,
            )
            response.raise_for_status()

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

    while not _stopping:
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
            if not _stopping:
                time.sleep(5)

    logger.info("Inference worker shutdown complete")


if __name__ == "__main__":
    main()
