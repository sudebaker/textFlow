#!/usr/bin/env python3
"""
Completion Worker for IA Text Orchestrator
Aggregates all results from the processing pipeline into a final JSON structure
"""

import os
import sys
import json
import logging
import time
import redis
import requests
from datetime import datetime
from typing import Dict, Any, Optional
from prometheus_client import Counter, Histogram, start_http_server

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
from pkg.events_python import EventBus

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
RESULTS_PATH = os.getenv("RESULTS_PATH", "/app/data/results")
API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8080")
METRICS_PORT = int(os.getenv("METRICS_PORT", "8005"))

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
    def __init__(self):
        self.redis_client = redis.from_url(REDIS_URL, decode_responses=True)
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
        webhook_url = WEBHOOK_URL
        if not webhook_url:
            return False

        try:
            payload = {
                "job_id": job_id,
                "status": status,
                "download_url": f"{API_BASE_URL}/v1/documents/{job_id}/download",
            }
            if error:
                payload["error"] = error

            response = requests.post(
                webhook_url,
                json=payload,
                timeout=10,
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
            logger.info(f"Webhook sent successfully for job {job_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to send webhook: {e}")
            return False

    def deduplicate_entities(self, entities: list) -> list:
        """
        Deduplicate entities using exact text match (not fuzzy).
        Keep all variations like "María Pérez" vs "María Pérez"
        Keep highest confidence for exact duplicates.
        """
        if not entities:
            return entities

        # Group by (label, exact text) - no fuzzy matching
        seen = {}
        result = []
        index_map = {}  # Maps key to index in result list

        for entity in entities:
            key = f"{entity.get('label', '')}:{entity.get('text', '')}"

            if key not in seen:
                # New unique entity
                index_map[key] = len(result)
                seen[key] = entity
                result.append(entity)
            else:
                # Exact match found - keep highest confidence
                existing = seen[key]
                if entity.get("confidence", 0) > existing.get("confidence", 0):
                    # Update in dictionary and result list
                    idx = index_map[key]
                    seen[key] = entity
                    result[idx] = entity

        logger.info(
            f"Deduplicated entities: {len(entities)} → {len(result)} "
            f"(removed {len(entities) - len(result)} exact duplicates)"
        )

        return result

    def get_job_creation_time(self, job_id: str) -> Optional[str]:
        meta = self.redis_client.hgetall(f"orchestrator:job:{job_id}:meta")
        created_at = meta.get("created_at")
        if created_at:
            return datetime.fromtimestamp(int(created_at)).isoformat()
        return None

    def check_job_completion(self, job_id: str):
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
                        logger.info(f"Job {job_id}: added 'inferences' to required_steps")
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
            pipe.get(f"orchestrator:job:{job_id}:embeddings")
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
                embeddings_json,
                entities_raw_json,
                source_classification_json,
                micro_inferences_json,
            ) = pipe.execute()

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

            embeddings_raw = json.loads(embeddings_json) if embeddings_json else {}
            embeddings = {"model": "BAAI/bge-m3", "dimension": 1024, **embeddings_raw}

            # Read RAW entities from entities-worker (before dedup)
            entities_raw = json.loads(entities_raw_json) if entities_raw_json else []

            # Apply deduplication at the end (now that we have all entities from all chunks)
            entities = self.deduplicate_entities(entities_raw) if entities_raw else []

            logger.info(
                f"Entities: {len(entities_raw)} raw → {len(entities)} after dedup"
            )

            # Parse source classification and micro inferences if present
            source_classification = None
            micro_inferences = None
            
            try:
                if source_classification_json:
                    source_classification = json.loads(source_classification_json)
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse source_classification JSON: {e}")
            
            try:
                if micro_inferences_json:
                    micro_inferences = json.loads(micro_inferences_json)
                    # micro_inferences is now: [{chunk_id: "...", inferences: [...]}, ...]
                    # Validate structure
                    if not isinstance(micro_inferences, list):
                        logger.warning(f"micro_inferences is not a list, got {type(micro_inferences)}")
                        micro_inferences = None
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse micro_inferences JSON: {e}")
                micro_inferences = None

            # Build results dict with optional inference fields
            results = {
                "job_id": job_id,
                "status": "completed",
                "created_at": created_at,
                "completed_at": completed_at,
                "document_metadata": document_metadata,
                "text_metadata": text_metadata,
                "chunks": chunks,
                "embeddings": embeddings,
                "entities": entities,
            }

            # Add optional inference fields if present
            if source_classification is not None:
                results["source_classification"] = source_classification
            if micro_inferences is not None:
                results["micro_inferences"] = micro_inferences

            # Log completion stats
            log_message = f"Job {job_id} finalized: chunks={len(chunks)}, entities={len(entities)}"
            if source_classification:
                log_message += f", source_type={source_classification.get('document_type', 'unknown')}"
            if micro_inferences:
                # micro_inferences is now a list of {chunk_id, inferences: [...]}
                if isinstance(micro_inferences, list):
                    total_inferences = sum(len(c.get("inferences", [])) for c in micro_inferences)
                    log_message += f", micro_inferences={total_inferences} (from {len(micro_inferences)} chunks)"
                else:
                    log_message += f", micro_inferences=<invalid structure>"
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
        """Start the worker and listen for job events with reconnection logic."""
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
