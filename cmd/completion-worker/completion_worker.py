#!/usr/bin/env python3
"""Completion Worker: Final aggregator in the textFlow pipeline.

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
import msgpack
import time
import redis
import requests
from datetime import datetime
from typing import Dict, Any, Optional, List

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
sys.path.insert(0, os.path.dirname(__file__))
from app.config.settings import Settings
from pkg.worker_common.artifact_store import STORE, resolve, resolve_text
from pkg.worker_common.entity_utils import deduplicate_entities
from pkg.worker_common.inference_embeddings import generate_inference_embeddings
from pkg.worker_common.pipeline_config import PipelineDefinition
from pkg.worker_common.pubsub_base import BasePubSubWorker

try:
    from sentence_transformers import SentenceTransformer
    import torch
    SENTENCE_TRANSFORMERS_AVAILABLE = True
except ImportError as e:
    SENTENCE_TRANSFORMERS_AVAILABLE = False
    SentenceTransformer = None
    torch = None

_settings = Settings()

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
RESULTS_PATH = os.getenv("RESULTS_PATH", "/app/data/results")
API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8080")
METRICS_PORT = int(os.getenv("METRICS_PORT", "8005"))
FUZZY_MATCH_THRESHOLD: float = _settings.fuzzy_match_threshold

EMBEDDINGS_MODEL_PATH = os.getenv("EMBEDDINGS_MODEL_PATH", "/models/bge-m3")
EMBEDDINGS_DEVICE = os.getenv("EMBEDDINGS_DEVICE", "cuda")
EMBEDDING_BATCH_SIZE = int(os.getenv("EMBEDDING_BATCH_SIZE", "32"))


class CompletionWorker(BasePubSubWorker):
    """Aggregates job results and finalizes document processing.

    This worker subscribes to job progress events via Redis pub/sub and monitors
    the completion status of all required pipeline steps. Once all required steps
    for a job are complete, it aggregates their results, saves to file, and sends
    webhook notifications.

    Attributes:
        redis_client: Redis client for pub/sub and data retrieval.
        event_bus: EventBus instance for publishing job completion/failure events.
        pipeline: PipelineDefinition with the declarative DAG steps for each
            pipeline variant (full, spreadsheet) and feature extras.
    """

    def __init__(self):
        super().__init__(
            worker_name="completion-worker",
            metrics_port=METRICS_PORT,
        )
        self.pipeline = PipelineDefinition.load()
        self._embedding_service = None
        self._embedding_service_loaded = False

    def _get_embedding_service(self):
        """Lazy loading of embedding service to avoid blocking startup."""
        if self._embedding_service_loaded:
            return self._embedding_service

        if not SENTENCE_TRANSFORMERS_AVAILABLE:
            self.logger.warning("sentence-transformers not available, inference embeddings disabled")
            self._embedding_service_loaded = True
            return None

        try:
            device = EMBEDDINGS_DEVICE if torch.cuda.is_available() else "cpu"
            self._embedding_service = SentenceTransformer(
                EMBEDDINGS_MODEL_PATH,
                device=device
            )
            self.logger.info(f"Embedding service loaded with device={device}")
        except Exception as e:
            self.logger.warning(f"Failed to load embedding service: {e}")
            self._embedding_service = None

        self._embedding_service_loaded = True
        return self._embedding_service

    def _generate_inference_embeddings(
        self, inferences_by_chunk: Dict[str, List[Dict]]
    ) -> Dict[str, Dict[str, List[float]]]:
        """Generate embeddings for inference texts.

        Args:
            inferences_by_chunk: Dict mapping chunk_id to list of inference dicts

        Returns:
            Dict mapping chunk_id to {inference_idx: embedding_vector}
        """
        if not self._get_embedding_service() or not inferences_by_chunk:
            return {}

        inference_embeddings: Dict[str, Dict[str, List[float]]] = {}

        for chunk_id, inferences in inferences_by_chunk.items():
            if not inferences:
                continue

            texts = [inf.get("text", "") or "" for inf in inferences]
            if not any(texts):
                continue

            try:
                embeddings = self._get_embedding_service().encode(
                    texts,
                    batch_size=EMBEDDING_BATCH_SIZE,
                    show_progress_bar=False,
                    convert_to_numpy=True,
                )

                chunk_embeddings: Dict[str, List[float]] = {}
                for idx, embedding in enumerate(embeddings):
                    chunk_embeddings[f"inference_{idx}"] = embedding.tolist() if hasattr(embedding, 'tolist') else list(embedding)

                inference_embeddings[chunk_id] = chunk_embeddings
                self.logger.debug(f"Generated {len(chunk_embeddings)} inference embeddings for chunk {chunk_id}")
            except Exception as e:
                self.logger.warning(f"Failed to generate embeddings for chunk {chunk_id}: {e}")

        return inference_embeddings

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
            self.logger.info(f"Results saved to {file_path}")
            return True
        except Exception as e:
            self.logger.error(f"Failed to save results to file: {e}")
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
            self.logger.info(f"Webhook sent successfully for job {job_id}")
            return True
        except Exception as e:
            self.logger.error(f"Failed to send webhook: {e}")
            return False

    def _check_and_notify_batch(self, job_id: str, status: str):
        """Check if job is part of a batch and notify when batch completes."""
        batch_id = self.redis_client.hget(
            f"orchestrator:job:{job_id}:meta", "batch_id"
        )
        if not batch_id:
            return

        batch_id = batch_id.decode() if isinstance(batch_id, bytes) else batch_id
        batch_done_key = f"orchestrator:batch:{batch_id}:done_count"

        done = self.redis_client.incr(batch_done_key)
        if done == 1:
            self.redis_client.expire(batch_done_key, 24 * 60 * 60)

        total = int(self.redis_client.hget(f"orchestrator:batch:{batch_id}:meta", "total") or 0)

        if done >= total:
            webhook_url = self.redis_client.hget(
                f"orchestrator:batch:{batch_id}:meta", "webhook_url"
            )
            webhook_secret = self.redis_client.hget(
                f"orchestrator:batch:{batch_id}:meta", "webhook_secret"
            )
            if webhook_url:
                webhook_url = webhook_url.decode() if isinstance(webhook_url, bytes) else webhook_url
                webhook_secret = webhook_secret.decode() if isinstance(webhook_secret, bytes) else webhook_secret
                self._send_batch_webhook(batch_id, status, webhook_url, webhook_secret)

    def _send_batch_webhook(self, batch_id: str, final_status: str, webhook_url: str, webhook_secret: Optional[str] = None):
        """Send batch completion webhook."""
        try:
            jobs = self.redis_client.smembers(f"orchestrator:batch:{batch_id}:jobs")
            job_statuses = []
            completed = failed = 0

            for job_id in jobs:
                job_id = job_id.decode() if isinstance(job_id, bytes) else job_id
                if not job_id:
                    continue
                status = self.redis_client.hget(f"orchestrator:job:{job_id}:status", "status")
                status = status.decode() if isinstance(status, bytes) else status
                job_statuses.append({"id": job_id, "status": status})
                if status == "completed":
                    completed += 1
                else:
                    failed += 1

            if failed == len(jobs):
                batch_status = "failed"
            elif failed > 0:
                batch_status = "partial"
            else:
                batch_status = "completed"

            payload = {
                "batch_id": batch_id,
                "status": batch_status,
                "total": len(jobs),
                "completed": completed,
                "failed": failed,
                "jobs": job_statuses,
            }

            headers = {"Content-Type": "application/json"}
            if webhook_secret:
                signature = hmac.new(
                    webhook_secret.encode(),
                    json.dumps(payload).encode(),
                    hashlib.sha256
                ).hexdigest()
                headers["X-Webhook-Signature"] = f"sha256={signature}"

            response = requests.post(webhook_url, json=payload, timeout=10, headers=headers)
            response.raise_for_status()
            self.logger.info(f"Batch webhook sent for {batch_id}")
        except Exception as e:
            self.logger.error(f"Failed to send batch webhook: {e}")

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

            self.logger.info(f"Job {job_id} completed steps: {completed_steps}")

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

            # Check if it's an audio job (has audio step instead of extraction)
            is_audio = "audio" in completed_steps

            features_json = self.redis_client.get(f"orchestrator:job:{job_id}:features")
            features = []
            if features_json:
                try:
                    features = json.loads(features_json)
                except Exception as e:
                    self.logger.warning(f"Failed to parse features: {e}")

            required_steps = self.pipeline.steps_for(
                is_spreadsheet=is_spreadsheet,
                is_audio=is_audio,
                features=features,
            )

            self.logger.info(
                f"Job {job_id} document type: {'spreadsheet' if is_spreadsheet else 'full'}, "
                f"required steps: {required_steps}"
            )

            if required_steps.issubset(completed_steps):
                self.finalize_job(job_id)

        except Exception as e:
            self.logger.error(f"Error checking job completion: {e}")

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
            self.logger.info(f"Finalizing job: {job_id}")

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
            embeddings_raw_bytes = resolve(
                STORE, self.redis_raw.get(f"orchestrator:job:{job_id}:embeddings")
            )
            inference_embeddings_raw = resolve(
                STORE, self.redis_raw.get(f"orchestrator:job:{job_id}:inference_embeddings")
            )

            created_at_timestamp = int(meta.get("created_at", time.time()))
            created_at = datetime.fromtimestamp(created_at_timestamp).isoformat()
            completed_at = datetime.fromtimestamp(int(time.time())).isoformat()

            if status_data and status_data.get("status") == "completed":
                self.logger.info(f"Job {job_id} already finalized, skipping")
                return

            text = resolve_text(STORE, text) or ""

            document_metadata = (
                json.loads(document_metadata_json) if document_metadata_json else {}
            )

            text_metadata = json.loads(text_metadata_json) if text_metadata_json else {}

            chunks_json = resolve_text(STORE, chunks_json)
            chunks = json.loads(chunks_json) if chunks_json else []

            # --- Embeddings: {chunk_id: [float]} ---
            embeddings_by_chunk: dict = {}
            if embeddings_raw_bytes:
                raw = msgpack.unpackb(embeddings_raw_bytes, raw=False)
                # raw is {chunk_id: [float]} — filter out any non-list values
                embeddings_by_chunk = {k: v for k, v in raw.items() if isinstance(v, list)}

            # --- Inference Embeddings: {chunk_id: {inference_idx: [float]}} ---
            inference_embeddings_by_chunk: dict = {}
            if inference_embeddings_raw:
                raw = msgpack.unpackb(inference_embeddings_raw, raw=False)
                inference_embeddings_by_chunk = {
                    k: v for k, v in raw.items() if isinstance(v, dict)
                }

            # --- Entities: deduplicate → global dict {entity_id: {label, text, confidence}} ---
            entities_raw = json.loads(entities_raw_json) if entities_raw_json else []
            entities_dict = (
                deduplicate_entities(entities_raw, threshold=FUZZY_MATCH_THRESHOLD)
                if entities_raw
                else {}
            )

            self.logger.info(
                f"Entities: {len(entities_raw)} raw → {len(entities_dict)} unique (by entity_id)"
            )

            # --- Build per-chunk entity_ids index (from deduplicated dict) ---
            entity_ids_by_chunk: dict = {}  # {chunk_id: [entity_id]}
            for eid, ent in entities_dict.items():
                cid = ent.get("chunk_id")
                if cid:
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
                self.logger.warning(f"Failed to parse source_classification JSON: {e}")

            try:
                if micro_inferences_json:
                    micro_inferences_list = json.loads(micro_inferences_json)
                    if isinstance(micro_inferences_list, list):
                        for item in micro_inferences_list:
                            cid = item.get("chunk_id")
                            if cid:
                                inferences_by_chunk[cid] = item.get("inferences", [])
                    else:
                        self.logger.warning(
                            f"micro_inferences is not a list, got {type(micro_inferences_list)}"
                        )
            except json.JSONDecodeError as e:
                self.logger.warning(f"Failed to parse micro_inferences JSON: {e}")

            # --- Generate inference embeddings ONLY if feature is enabled ---
            # Read features from Redis to check if inference_embeddings is enabled
            features_json = self.redis_client.get(f"orchestrator:job:{job_id}:features")
            features = []
            if features_json:
                try:
                    features = json.loads(features_json)
                except Exception:
                    pass

            inference_embeddings_enabled = "inference_embeddings" in features and "inferences" in features

            if inference_embeddings_enabled:
                if not inference_embeddings_by_chunk and inferences_by_chunk and self._get_embedding_service():
                    self.logger.info("Feature 'inference_embeddings' enabled, generating embeddings...")
                    inference_embeddings_by_chunk = self._generate_inference_embeddings(inferences_by_chunk)
                    if inference_embeddings_by_chunk:
                        try:
                            key = f"orchestrator:job:{job_id}:inference_embeddings"
                            ie_ref = STORE.put(msgpack.packb(inference_embeddings_by_chunk, use_bin_type=True))
                            self.redis_raw.set(key, ie_ref.encode("utf-8"))
                            self.logger.info(f"Saved inference embeddings to Redis: {key}")
                        except Exception as e:
                            self.logger.warning(f"Failed to save inference embeddings to Redis: {e}")
            else:
                self.logger.debug(f"Feature 'inference_embeddings' not enabled, skipping embedding generation")

            # --- Enrich chunks: embed embeddings, entity_ids, inferences ---
            enriched_chunks = []
            for chunk in chunks:
                cid = chunk.get("chunk_id", "")
                enriched = dict(chunk)  # shallow copy — preserve all existing fields
                enriched["embeddings"] = embeddings_by_chunk.get(cid, [])
                enriched["entity_ids"] = entity_ids_by_chunk.get(cid, [])

                # Enrich each inference with its embedding
                inferences = inferences_by_chunk.get(cid, [])
                chunk_inf_emb = inference_embeddings_by_chunk.get(cid, {})
                for idx, inf in enumerate(inferences):
                    inf_copy = dict(inf)
                    emb_key = f"inference_{idx}"
                    if emb_key in chunk_inf_emb:
                        inf_copy["embedding"] = chunk_inf_emb[emb_key]
                    inferences[idx] = inf_copy

                if inferences and chunk_inf_emb:
                    expected = len(inferences)
                    actual = len(chunk_inf_emb)
                    if expected != actual:
                        self.logger.warning(
                            f"Embedding count mismatch for chunk {cid}: "
                            f"expected {expected}, got {actual}"
                        )

                enriched["inferences"] = inferences
                enriched_chunks.append(enriched)

            # --- Final result ---
            results = {
                "job_id": job_id,
                "status": "completed",
                "created_at": created_at,
                "completed_at": completed_at,
                "text": text,
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
            self.logger.info(log_message)

            self.redis_client.hset(
                f"orchestrator:job:{job_id}:meta", "completed_at", str(int(time.time()))
            )

            self.redis_client.hset(
                f"orchestrator:job:{job_id}:status", "status", "completed"
            )

            self.save_results_to_file(job_id, results)
            self.send_webhook(job_id, "completed", None)
            self._check_and_notify_batch(job_id, "completed")

            self.event_bus.publish_job_completed(job_id)

            # Record metrics
            self.job_duration.observe(time.time() - finalization_start_time)
            self.jobs_total.labels(status="success").inc()

        except Exception as e:
            self.logger.error(f"Error finalizing job: {e}", exc_info=True)
            self.redis_client.hset(
                f"orchestrator:job:{job_id}:status", "status", "failed"
            )
            self.redis_client.set(
                f"orchestrator:job:{job_id}:error", f"Finalization error: {str(e)}"
            )
            self.send_webhook(job_id, "failed", str(e))
            self.event_bus.publish_job_failed(job_id, str(e))

            # Record failure metrics
            self.job_duration.observe(time.time() - finalization_start_time)
            self.jobs_total.labels(status="error").inc()

    def handle_event(self, event: Dict[str, Any]) -> None:
        """Process parsed job progress event from Redis pub/sub.

        The event dict is already parsed by BasePubSubWorker._parse_pubsub_message().
        """
        try:
            event_type = event.get("event_type")
            job_id = event.get("job_id")

            self.logger.info(f"Received event: {event_type} for job {job_id}")

            if event_type == "job_progress" and job_id:
                self.check_job_completion(job_id)

        except Exception as e:
            self.logger.error(f"Error handling event: {e}")


def main():
    worker = CompletionWorker()
    worker.start()


if __name__ == "__main__":
    main()
