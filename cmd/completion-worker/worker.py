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
from datetime import datetime
from typing import Dict, Any, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
from pkg.events_python import EventBus

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


class CompletionWorker:
    def __init__(self):
        self.redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        self.event_bus = EventBus(self.redis_client)
        self.required_steps = {"extraction", "embeddings", "entities", "metadata"}

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

        for entity in entities:
            key = f"{entity.get('label', '')}:{entity.get('text', '')}"

            if key not in seen:
                # New unique entity
                seen[key] = entity
                result.append(entity)
            else:
                # Exact match found - keep highest confidence
                existing = seen[key]
                if entity.get("confidence", 0) > existing.get("confidence", 0):
                    # Update in dictionary
                    seen[key] = entity
                    # Update in result list
                    idx = result.index(existing)
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

            if self.required_steps.issubset(completed_steps):
                self.finalize_job(job_id)

        except Exception as e:
            logger.error(f"Error checking job completion: {e}")

    def finalize_job(self, job_id: str):
        try:
            logger.info(f"Finalizing job: {job_id}")

            meta = self.redis_client.hgetall(f"orchestrator:job:{job_id}:meta")
            created_at_timestamp = int(meta.get("created_at", time.time()))
            created_at = datetime.fromtimestamp(created_at_timestamp).isoformat()
            completed_at = datetime.fromtimestamp(int(time.time())).isoformat()

            status_data = self.redis_client.hgetall(f"orchestrator:job:{job_id}:status")
            if status_data and status_data.get("status") == "completed":
                logger.info(f"Job {job_id} already finalized, skipping")
                return

            text = self.redis_client.get(f"orchestrator:job:{job_id}:text") or ""

            document_metadata_json = self.redis_client.get(
                f"orchestrator:job:{job_id}:metadata:document"
            )
            document_metadata = (
                json.loads(document_metadata_json) if document_metadata_json else {}
            )

            text_metadata_json = self.redis_client.get(
                f"orchestrator:job:{job_id}:metadata:text"
            )
            text_metadata = json.loads(text_metadata_json) if text_metadata_json else {}

            chunks_json = self.redis_client.get(f"orchestrator:job:{job_id}:chunks")
            chunks = json.loads(chunks_json) if chunks_json else []

            embeddings_json = self.redis_client.get(
                f"orchestrator:job:{job_id}:embeddings"
            )
            embeddings_raw = json.loads(embeddings_json) if embeddings_json else {}
            embeddings = {"model": "BAAI/bge-m3", "dimension": 1024, **embeddings_raw}

            # Read RAW entities from entities-worker (before dedup)
            entities_raw_json = self.redis_client.get(
                f"orchestrator:job:{job_id}:entities_raw"
            )
            entities_raw = json.loads(entities_raw_json) if entities_raw_json else []

            # Apply deduplication at the end (now that we have all entities from all chunks)
            entities = self.deduplicate_entities(entities_raw) if entities_raw else []

            logger.info(
                f"Entities: {len(entities_raw)} raw → {len(entities)} after dedup"
            )

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

            self.event_bus.publish_job_completed(job_id)

            logger.info(
                f"Job {job_id} finalized: chunks={len(chunks)}, entities={len(entities)}"
            )

        except Exception as e:
            logger.error(f"Error finalizing job: {e}", exc_info=True)
            self.redis_client.hset(
                f"orchestrator:job:{job_id}:status", "status", "failed"
            )
            self.redis_client.set(
                f"orchestrator:job:{job_id}:error", f"Finalization error: {str(e)}"
            )
            self.event_bus.publish_job_failed(job_id, str(e))

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
        pubsub = self.redis_client.pubsub()
        pubsub.subscribe("job:events")

        logger.info("Completion worker started, listening for job events...")

        for message in pubsub.listen():
            self.handle_event(message)


def main():
    worker = CompletionWorker()
    worker.start()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    main()
