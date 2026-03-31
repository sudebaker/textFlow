#!/usr/bin/env python3
"""Completion Worker: Final aggregator in the IA Text Orchestrator pipeline.

This module monitors job completion across all workers via Redis pub/sub and
aggregates their results into a single finalized JSON structure. It acts as the
final step in the document processing pipeline, waiting for all required workers
(extraction, embeddings, entities, metadata) to complete before writing results
to file and notifying via webhook.

Key responsibilities:
  - Subscribe to job:events channel via Redis pub/sub
  - Monitor completion status of all pipeline workers for each job
  - Deduplicate entities and aggregate results from all workers
  - Finalize jobs by saving results to /results/{job_id}.json
  - Send webhook notifications when jobs complete or fail
  - Support different pipeline types (default full pipeline vs. spreadsheet)

Environment variables:
  - REDIS_URL: Redis connection URL (default: redis://localhost:6379)
  - WEBHOOK_URL: Optional webhook endpoint for job completion notifications
  - RESULTS_PATH: Directory to save final JSON results (default: /app/data/results)
  - API_BASE_URL: Base URL for download links in webhook (default: http://localhost:8080)
  - METRICS_PORT: Prometheus metrics port (default: 8005)

Metrics:
  - completion_worker_jobs_finalized_total: Counter of finalized jobs by status
  - completion_worker_job_finalization_duration_seconds: Histogram of finalization time

Pipeline variants:
  - Full pipeline (default): extraction, embeddings, entities, metadata
  - Spreadsheet pipeline: extraction, entities (skips embeddings/metadata)
  - With inferences: Adds 'inferences' to required_steps if feature was requested
"""

import os
import sys
import json
import hashlib
import hmac
import logging
import msgpack
import time
import redis
import requests
from datetime import datetime
from typing import Dict, Any, Optional
from prometheus_client import Counter, Histogram, start_http_server
from rapidfuzz import fuzz
from unidecode import unidecode

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
sys.path.insert(0, os.path.dirname(__file__))
from pkg.events_python import EventBus
from app.config.settings import Settings

_settings = Settings()

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
RESULTS_PATH = os.getenv("RESULTS_PATH", "/app/data/results")
API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8080")
METRICS_PORT = int(os.getenv("METRICS_PORT", "8005"))
FUZZY_MATCH_THRESHOLD: float = _settings.fuzzy_match_threshold

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Prometheus metrics
jobs_finalized_total = Counter(
    "completion_worker_jobs_finalized_total", "Total jobs finalized", ["status"]
)
job_finalization_duration = Histogram(
    "completion_worker_job_finalization_duration_seconds",
    "Job finalization duration in seconds",
)


class CompletionWorker:
    """Aggregates job results and finalizes document processing.

    This worker subscribes to job progress events via Redis pub/sub and monitors
    the completion status of all required pipeline steps. Once all required steps
    for a job are complete, it aggregates their results, saves to file, and sends
    webhook notifications.

    Attributes:
        redis_client: Redis client for pub/sub and data retrieval.
        event_bus: EventBus instance for publishing job completion/failure events.
        default_required_steps: Set of steps required for full pipeline jobs
            (extraction, embeddings, entities, metadata).
        spreadsheet_required_steps: Set of steps required for spreadsheet jobs
            (extraction, entities only).
    """

    def __init__(self):
        """Initialize the completion worker with Redis connection and event bus.

        Sets up Redis client for pub/sub subscriptions and data retrieval, and
        defines which pipeline steps are required for different document types.
        Different pipeline variants have different required steps:
          - Full pipeline (default): extraction, embeddings, entities, metadata
          - Spreadsheet: extraction, entities (skips embeddings/metadata)
          - With inferences: Adds 'inferences' to required_steps if feature requested

        Raises:
            redis.ConnectionError: If Redis connection cannot be established.
        """
        self.redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        # Raw client (no decode_responses) for binary keys like MsgPack embeddings
        self.redis_raw = redis.from_url(REDIS_URL, decode_responses=False)
        self.event_bus = EventBus(self.redis_client)
        # Default required steps for full pipeline
        self.default_required_steps = {
            "extraction",
            "embeddings",
            "entities",
            "metadata",
        }
        # Spreadsheet pipeline (no embeddings, no metadata)
        self.spreadsheet_required_steps = {"extraction", "entities"}

    def save_results_to_file(self, job_id: str, results: Dict[str, Any]) -> bool:
        """Save finalized job results to JSON file.

        Writes the complete aggregated results to /results/{job_id}.json with
        proper UTF-8 encoding and pretty-printing (indent=2) for readability.
        Automatically creates the RESULTS_PATH directory if it doesn't exist.

        This operation is idempotent and can be safely called multiple times
        for the same job (it will overwrite the previous file).

        Args:
            job_id: Unique identifier of the job.
            results: Dictionary containing aggregated JobResults with keys:
                - job_id, status, created_at, completed_at
                - document_metadata, text_metadata
                - chunks, embeddings, entities
                - (optional) source_classification, micro_inferences

        Returns:
            True if file was saved successfully, False if an exception occurred.
            Exceptions are logged but not raised.

        Raises:
            Does not raise exceptions; logs errors and returns False instead.
        """
        try:
            os.makedirs(RESULTS_PATH, exist_ok=True)
            file_path = os.path.join(RESULTS_PATH, f"{job_id}.json")
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(results, f, ensure_ascii=False, indent=2)
            logger.info(f"Results saved to {file_path}")
            return True
        except Exception as e:
            logger.error(f"Failed to save results to file: {e}")
            return False

    def send_webhook(
        self, job_id: str, status: str, error: Optional[str] = None
    ) -> bool:
        """Send webhook notification when job completes or fails.

        Posts a notification to the configured WEBHOOK_URL with job completion
        or failure details. The webhook is only sent if WEBHOOK_URL environment
        variable is set or per-job webhook is configured; if not configured,
        this method returns False silently.

        Per-job webhooks are read from Redis :meta hash (webhook_url, webhook_secret).
        Falls back to global WEBHOOK_URL if no per-job webhook is set.

        When webhook_secret is present, computes HMAC-SHA256 signature using:
            signature = hmac.new(secret.encode(), json.dumps(payload).encode(), hashlib.sha256).hexdigest()
        Adds headers X-Webhook-Signature and X-Webhook-Timestamp.

        Webhook payload structure:
            {
                "job_id": str,
                "status": "completed" | "failed",
                "download_url": f"{API_BASE_URL}/v1/documents/{job_id}/download",
                "completed_at": str (ISO 8601 format),
                "error": str (only if status="failed")
            }

        This operation is idempotent (can be called multiple times for same job).
        Failures are logged but do not prevent job finalization.

        Args:
            job_id: Unique identifier of the job.
            status: Job status ("completed" or "failed").
            error: Optional error message to include in webhook payload
                (only applicable if status="failed").

        Returns:
            True if webhook was sent successfully or WEBHOOK_URL not configured.
            False if webhook_url is set but the HTTP request failed.

        Raises:
            Does not raise exceptions; logs errors and returns False instead.

        Note:
            - Webhook timeout is 10 seconds
            - HTTP errors are logged at ERROR level
            - Optional WEBHOOK_URL env var or per-job webhook determines if webhooks are enabled
        """
        webhook_url = WEBHOOK_URL
        webhook_secret = ""

        if job_id:
            meta = self.redis_client.hgetall(f"orchestrator:job:{job_id}:meta")
            job_webhook_url = meta.get("webhook_url", "")
            if job_webhook_url:
                webhook_url = job_webhook_url
                webhook_secret = meta.get("webhook_secret", "")

        if not webhook_url:
            return False

        try:
            payload = {
                "job_id": job_id,
                "status": status,
                "download_url": f"{API_BASE_URL}/v1/documents/{job_id}/download",
                "completed_at": datetime.utcnow().isoformat() + "Z",
            }
            if error:
                payload["error"] = error

            headers = {"Content-Type": "application/json"}
            if webhook_secret:
                timestamp = str(int(time.time()))
                payload_json = json.dumps(payload, sort_keys=True)
                signature = hmac.new(
                    webhook_secret.encode(),
                    payload_json.encode(),
                    hashlib.sha256
                ).hexdigest()
                headers["X-Webhook-Signature"] = signature
                headers["X-Webhook-Timestamp"] = timestamp

            response = requests.post(
                webhook_url,
                json=payload,
                timeout=10,
                headers=headers,
            )
            response.raise_for_status()
            logger.info(f"Webhook sent successfully for job {job_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to send webhook: {e}")
            return False

    def deduplicate_entities(self, entities: list) -> dict:
        """Deduplicate entities using fuzzy text matching, keeping highest confidence.

        Two entities merge when they share the same label AND their normalized texts
        are similar enough (fuzz.ratio >= FUZZY_MATCH_THRESHOLD).  Normalization uses
        unidecode + lower + strip so accented variants ("Educación" / "Educacion")
        are treated as identical.

        The threshold is read from FUZZY_MATCH_THRESHOLD env var (default 0.85).

        Args:
            entities: List of entity dicts, each expected to have:
                - entity_id (optional): stable 12-char hex ID
                - label, text, confidence

        Returns:
            Dict keyed by entity_id → {label, text, confidence}.
            Per-chunk fields (chunk_id, start, end) are preserved as start_offset,
            end_offset, chunk_id in the merged entity.
            Falls back to generating entity_id from label:text if field missing.
        """
        if not entities:
            return {}

        def _normalize(text: str) -> str:
            return unidecode(text).lower().strip()

        def _generate_id(label: str, text: str) -> str:
            key = f"{label}:{_normalize(text)}"
            return hashlib.sha256(key.encode()).hexdigest()[:12]

        # result maps entity_id → {label, text, confidence}
        result: dict = {}
        # norm_index maps entity_id → normalized text (for similarity lookup)
        norm_index: dict = {}

        for ent in entities:
            label = ent.get("label", "")
            text = ent.get("text", "")
            confidence = ent.get("confidence", 0.0)
            norm_text = _normalize(text)

            # Find an existing entry with same label and similar enough text
            matched_id = None
            for existing_id, existing_norm in norm_index.items():
                if result[existing_id]["label"] != label:
                    continue
                similarity = fuzz.ratio(norm_text, existing_norm) / 100.0
                if similarity >= FUZZY_MATCH_THRESHOLD:
                    matched_id = existing_id
                    break

            if matched_id:
                if confidence > result[matched_id].get("confidence", 0):
                    result[matched_id] = {
                        "label": label,
                        "text": text,
                        "confidence": confidence,
                        "start_offset": ent.get("start", 0),
                        "end_offset": ent.get("end", 0),
                        "chunk_id": ent.get("chunk_id", ""),
                    }
                    norm_index[matched_id] = _normalize(text)
            else:
                eid = ent.get("entity_id") or _generate_id(label, text)
                result[eid] = {
                    "label": label,
                    "text": text,
                    "confidence": confidence,
                    "start_offset": ent.get("start", 0),
                    "end_offset": ent.get("end", 0),
                    "chunk_id": ent.get("chunk_id", ""),
                }
                norm_index[eid] = norm_text

        logger.info(
            f"Deduplicated entities: {len(entities)} raw → {len(result)} unique"
            f" (threshold={FUZZY_MATCH_THRESHOLD})"
        )
        return result

    def get_job_creation_time(self, job_id: str) -> Optional[str]:
        """Retrieve the ISO 8601 creation timestamp for a job.

        Fetches the job's created_at timestamp from Redis metadata and converts
        it to ISO 8601 format for inclusion in final results.

        Args:
            job_id: Unique identifier of the job.

        Returns:
            ISO 8601 formatted timestamp string (e.g., "2024-03-27T10:30:45"),
            or None if metadata not found or created_at is not set.
        """
        meta = self.redis_client.hgetall(f"orchestrator:job:{job_id}:meta")
        created_at = meta.get("created_at")
        if created_at:
            return datetime.fromtimestamp(int(created_at)).isoformat()
        return None

    def check_job_completion(self, job_id: str):
        """Poll Redis to check if all required pipeline steps are completed.

        Determines which steps are required based on document type and features:
          - Full pipeline (default): extraction, embeddings, entities, metadata
          - Spreadsheet: extraction, entities (MIME type check)
          - With inferences: Adds 'inferences' if feature was requested

        If all required steps have completed status, triggers finalize_job()
        to aggregate results and save to file.

        Document type detection:
            Spreadsheets detected by MIME type:
            - application/vnd.openxmlformats-officedocument.spreadsheetml.sheet
            - application/vnd.ms-excel
            - text/csv
            - application/zip (Excel files may show as ZIP)

        Feature detection:
            If job:features JSON contains "inferences" key, adds it to
            required_steps set.

        Args:
            job_id: Unique identifier of the job.

        Returns:
            None. This method is fire-and-forget; if completion is detected,
            it calls finalize_job() as a side effect.

        Raises:
            Does not raise exceptions; errors are logged and processing continues.
        """
        try:
            steps = self.redis_client.hgetall(f"orchestrator:job:{job_id}:steps")

            completed_steps = set()
            for step, status in steps.items():
                if status == "completed":
                    completed_steps.add(step)

            logger.info(f"Job {job_id} completed steps: {completed_steps}")

            # Determine required steps based on document type and features
            document_metadata_json = self.redis_client.get(
                f"orchestrator:job:{job_id}:metadata:document"
            )
            document_metadata = (
                json.loads(document_metadata_json) if document_metadata_json else {}
            )
            mime_type = document_metadata.get("mime_type", "")

            # Check if it's a spreadsheet
            is_spreadsheet = "spreadsheet" in mime_type.lower() or mime_type in [
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "application/vnd.ms-excel",
                "text/csv",
                "application/zip",  # Excel files may show as ZIP
            ]

            required_steps = (
                self.spreadsheet_required_steps
                if is_spreadsheet
                else self.default_required_steps.copy()
            )

            # Add inferences if features were requested
            features_json = self.redis_client.get(f"orchestrator:job:{job_id}:features")
            logger.debug(f"Job {job_id}: features_json={features_json}")
            if features_json:
                try:
                    features = json.loads(features_json)
                    if "inferences" in features:
                        required_steps.add("inferences")
                        logger.info(
                            f"Job {job_id}: added 'inferences' to required_steps"
                        )
                except Exception as e:
                    logger.warning(f"Failed to parse features: {e}")

            logger.info(
                f"Job {job_id} document type: {'spreadsheet' if is_spreadsheet else 'full'}, "
                f"required steps: {required_steps}"
            )

            if required_steps.issubset(completed_steps):
                self.finalize_job(job_id)

        except Exception as e:
            logger.error(f"Error checking job completion: {e}")

    def finalize_job(self, job_id: str):
        """Aggregate all worker results and finalize a completed job.

        This is the final aggregation step that brings together results from
        all workers (extraction, embeddings, entities, metadata, inferences).

        Process:
            1. Fetch all job data from Redis in a single pipeline operation
            2. Check if job is not already finalized (idempotent check)
            3. Parse and aggregate results from each worker:
               - Deduplicate entities (keep highest confidence)
               - Normalize embeddings with model metadata
               - Parse optional source_classification and micro_inferences
            4. Save aggregated results to file (/results/{job_id}.json)
            5. Send webhook notification (if configured)
            6. Mark job status as "completed" in Redis
            7. Publish job_completed event

        Error handling:
            On any exception during finalization:
            - Job status marked as "failed"
            - Error message stored in Redis
            - Webhook notification sent with error details
            - Metrics recorded for failure case

        Redis keys read:
            - orchestrator:job:{job_id}:meta (job metadata)
            - orchestrator:job:{job_id}:status (current status)
            - orchestrator:job:{job_id}:text (raw text)
            - orchestrator:job:{job_id}:metadata:document (MIME type, etc.)
            - orchestrator:job:{job_id}:metadata:text (text metadata)
            - orchestrator:job:{job_id}:chunks (extracted chunks)
            - orchestrator:job:{job_id}:embeddings (vector embeddings)
            - orchestrator:job:{job_id}:entities_raw (raw entities before dedup)
            - orchestrator:job:{job_id}:source_classification (document classification)
            - orchestrator:job:{job_id}:micro_inferences (per-chunk inferences)

        Redis keys written:
            - orchestrator:job:{job_id}:results (final aggregated results)
            - orchestrator:job:{job_id}:meta (add completed_at timestamp)
            - orchestrator:job:{job_id}:status (mark status="completed")
            - orchestrator:job:{job_id}:error (on failure only)

        Args:
            job_id: Unique identifier of the job.

        Returns:
            None.

        Raises:
            Does not raise exceptions. All errors are caught, logged, and
            converted to job failure status.

        Side effects:
            - Writes /results/{job_id}.json file
            - Sends webhook notification (if WEBHOOK_URL configured)
            - Publishes job_completed or job_failed event
            - Updates Prometheus metrics

        Note:
            Idempotent: If job is already marked as "completed" status,
            function returns early without re-processing.
        """
        finalization_start_time = time.time()
        try:
            logger.info(f"Finalizing job: {job_id}")

            # Use Redis pipeline to fetch all required data in a single round-trip
            pipe = self.redis_client.pipeline()
            pipe.hgetall(f"orchestrator:job:{job_id}:meta")
            pipe.hgetall(f"orchestrator:job:{job_id}:status")
            pipe.get(f"orchestrator:job:{job_id}:text")
            pipe.get(f"orchestrator:job:{job_id}:metadata:document")
            pipe.get(f"orchestrator:job:{job_id}:metadata:text")
            pipe.get(f"orchestrator:job:{job_id}:chunks")
            pipe.get(f"orchestrator:job:{job_id}:entities_raw")
            pipe.get(f"orchestrator:job:{job_id}:source_classification")
            pipe.get(f"orchestrator:job:{job_id}:micro_inferences")
            (
                meta,
                status_data,
                text,
                document_metadata_json,
                text_metadata_json,
                chunks_json,
                entities_raw_json,
                source_classification_json,
                micro_inferences_json,
            ) = pipe.execute()

            # Embeddings are stored as MsgPack binary — use raw client (no decode_responses)
            embeddings_raw_bytes = self.redis_raw.get(f"orchestrator:job:{job_id}:embeddings")

            created_at_timestamp = int(meta.get("created_at", time.time()))
            created_at = datetime.fromtimestamp(created_at_timestamp).isoformat()
            completed_at = datetime.fromtimestamp(int(time.time())).isoformat()

            if status_data and status_data.get("status") == "completed":
                logger.info(f"Job {job_id} already finalized, skipping")
                return

            text = text or ""

            document_metadata = (
                json.loads(document_metadata_json) if document_metadata_json else {}
            )

            text_metadata = json.loads(text_metadata_json) if text_metadata_json else {}

            chunks = json.loads(chunks_json) if chunks_json else []

            # --- Embeddings: {chunk_id: [float]} ---
            embeddings_by_chunk: dict = {}
            if embeddings_raw_bytes:
                raw = msgpack.unpackb(embeddings_raw_bytes, raw=False)
                # raw is {chunk_id: [float]} — filter out any non-list values
                embeddings_by_chunk = {k: v for k, v in raw.items() if isinstance(v, list)}

            # --- Entities: deduplicate → global dict {entity_id: {label, text, confidence}} ---
            entities_raw = json.loads(entities_raw_json) if entities_raw_json else []
            entities_dict = self.deduplicate_entities(entities_raw) if entities_raw else {}

            logger.info(
                f"Entities: {len(entities_raw)} raw → {len(entities_dict)} unique (by entity_id)"
            )

            # --- Build per-chunk entity_ids index ---
            entity_ids_by_chunk: dict = {}  # {chunk_id: [entity_id]}
            for ent in entities_raw:
                cid = ent.get("chunk_id")
                eid = ent.get("entity_id")
                if cid and eid:
                    entity_ids_by_chunk.setdefault(cid, [])
                    if eid not in entity_ids_by_chunk[cid]:
                        entity_ids_by_chunk[cid].append(eid)

            # --- Micro-inferences: parse and index by chunk_id ---
            inferences_by_chunk: dict = {}  # {chunk_id: [inference]}
            source_classification = None

            try:
                if source_classification_json:
                    source_classification = json.loads(source_classification_json)
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse source_classification JSON: {e}")

            try:
                if micro_inferences_json:
                    micro_inferences_list = json.loads(micro_inferences_json)
                    if isinstance(micro_inferences_list, list):
                        for item in micro_inferences_list:
                            cid = item.get("chunk_id")
                            if cid:
                                inferences_by_chunk[cid] = item.get("inferences", [])
                    else:
                        logger.warning(
                            f"micro_inferences is not a list, got {type(micro_inferences_list)}"
                        )
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse micro_inferences JSON: {e}")

            # --- Enrich chunks: embed embeddings, entity_ids, inferences ---
            enriched_chunks = []
            for chunk in chunks:
                cid = chunk.get("chunk_id", "")
                enriched = dict(chunk)  # shallow copy — preserve all existing fields
                enriched["embeddings"] = embeddings_by_chunk.get(cid, [])
                enriched["entity_ids"] = entity_ids_by_chunk.get(cid, [])
                enriched["inferences"] = inferences_by_chunk.get(cid, [])
                enriched_chunks.append(enriched)

            # --- Final result ---
            results = {
                "job_id": job_id,
                "status": "completed",
                "created_at": created_at,
                "completed_at": completed_at,
                "document_metadata": document_metadata,
                "text_metadata": text_metadata,
                "chunks": enriched_chunks,
                "entities": entities_dict,
            }

            if source_classification is not None:
                results["source_classification"] = source_classification

            # Log completion stats
            total_inferences = sum(len(c.get("inferences", [])) for c in enriched_chunks)
            log_message = (
                f"Job {job_id} finalized: chunks={len(enriched_chunks)}, "
                f"entities={len(entities_dict)}, inferences={total_inferences}"
            )
            if source_classification:
                log_message += f", source_type={source_classification.get('document_type', 'unknown')}"
            logger.info(log_message)

            self.redis_client.set(
                f"orchestrator:job:{job_id}:results",
                json.dumps(results, ensure_ascii=False),
            )

            self.redis_client.hset(
                f"orchestrator:job:{job_id}:meta", "completed_at", str(int(time.time()))
            )

            self.redis_client.hset(
                f"orchestrator:job:{job_id}:status", "status", "completed"
            )

            self.save_results_to_file(job_id, results)
            self.send_webhook(job_id, "completed", None)

            self.event_bus.publish_job_completed(job_id)

            # Record metrics
            job_finalization_duration.observe(time.time() - finalization_start_time)
            jobs_finalized_total.labels(status="success").inc()

        except Exception as e:
            logger.error(f"Error finalizing job: {e}", exc_info=True)
            self.redis_client.hset(
                f"orchestrator:job:{job_id}:status", "status", "failed"
            )
            self.redis_client.set(
                f"orchestrator:job:{job_id}:error", f"Finalization error: {str(e)}"
            )
            self.send_webhook(job_id, "failed", str(e))
            self.event_bus.publish_job_failed(job_id, str(e))

            # Record failure metrics
            job_finalization_duration.observe(time.time() - finalization_start_time)
            jobs_finalized_total.labels(status="error").inc()

    def handle_event(self, message):
        """Process incoming job progress event from Redis pub/sub.

        This is the event handler called by Redis pub/sub listener for each
        message on the job:events channel. It parses the event, extracts the
        job_id and event_type, and triggers job completion checks if the
        event is of type "job_progress".

        Event structure:
            {
                "type": "message" (Redis pub/sub message type),
                "data": JSON string containing:
                    {
                        "event_type": "job_progress" | other,
                        "job_id": str,
                        ...other fields...
                    }
            }

        Behavior:
            - Ignores non-"message" type events (e.g., "subscribe" confirmations)
            - For "job_progress" events, calls check_job_completion(job_id)
            - Logs event receipt for debugging

        Args:
            message: Dictionary from Redis pub/sub listener with keys:
                - type: "message" | "subscribe" | "unsubscribe"
                - channel: "job:events"
                - data: JSON string containing event details

        Returns:
            None.

        Raises:
            Does not raise exceptions. Parsing errors are logged and ignored.

        Note:
            Exceptions during parsing or check_job_completion are caught
            and logged at ERROR level without stopping the pub/sub listener.
        """
        try:
            if message["type"] != "message":
                return

            event = json.loads(message["data"])
            event_type = event.get("event_type")
            job_id = event.get("job_id")

            logger.info(f"Received event: {event_type} for job {job_id}")

            if event_type == "job_progress" and job_id:
                self.check_job_completion(job_id)

        except Exception as e:
            logger.error(f"Error handling event: {e}")

    def start(self):
        """Start the completion worker and listen for job progress events.

        Main entry point for the worker. Subscribes to the job:events Redis
        pub/sub channel and begins processing job progress notifications.

        This method runs indefinitely and implements exponential backoff
        reconnection logic to handle Redis connection failures gracefully:
            - Initial backoff: 1 second
            - Exponential increase with each reconnection
            - Maximum backoff cap: 60 seconds

        Connection failures are logged but do not halt the worker; instead,
        the worker waits and reconnects automatically.

        Process:
            1. Connect to Redis and subscribe to job:events channel
            2. For each message received, call handle_event()
            3. On connection error, close pubsub connection and wait
            4. Reconnect with exponential backoff

        Returns:
            Never returns under normal operation; runs indefinitely.

        Raises:
            Does not raise exceptions. All errors are logged and handled
            with automatic reconnection.

        Side effects:
            - Logs "Completion worker started, listening for job events..."
            - Logs connection errors and reconnection delays
            - Calls handle_event() for each incoming message
        """
        backoff_time = 1
        max_backoff_time = 60

        while True:
            try:
                pubsub = self.redis_client.pubsub()
                pubsub.subscribe("job:events")

                logger.info("Completion worker started, listening for job events...")

                for message in pubsub.listen():
                    self.handle_event(message)

            except Exception as e:
                logger.error(f"Error in completion worker pubsub: {e}", exc_info=True)
                try:
                    pubsub.close()
                except Exception:
                    pass

                # Exponential backoff with max cap
                logger.info(f"Reconnecting in {backoff_time} seconds...")
                time.sleep(backoff_time)
                backoff_time = min(backoff_time * 2, max_backoff_time)


def main():
    """Entry point for the completion worker service.

    Initializes Prometheus metrics server and starts the CompletionWorker
    to listen for job progress events.

    Prometheus metrics are exposed on METRICS_PORT (default 8005) at:
        http://localhost:8005/metrics

    Returns:
        Never returns; runs indefinitely until process is terminated.

    Raises:
        Does not raise exceptions. All errors are handled within
        CompletionWorker.start() with automatic reconnection logic.
    """
    # Start Prometheus metrics server
    logger.info(f"Starting metrics server on port {METRICS_PORT}")
    start_http_server(METRICS_PORT)

    worker = CompletionWorker()
    worker.start()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    main()
