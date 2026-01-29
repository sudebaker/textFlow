#!/usr/bin/env python3
import os
import sys
import json
import logging
import time
import redis

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
from pkg.events_python import EventBus

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

logger = logging.getLogger(__name__)

class CompletionWorker:
    def __init__(self):
        self.redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        self.event_bus = EventBus(self.redis_client)
        self.required_steps = {"extraction", "embeddings", "entities", "metadata"}

    def check_job_completion(self, job_id: str):
        """Check if all required steps are completed"""
        try:
            # Get step statuses
            steps = self.redis_client.hgetall(f"orchestrator:job:{job_id}:steps")

            # Check if all required steps are completed
            completed_steps = set()
            for step, status in steps.items():
                if status == "completed":
                    completed_steps.add(step)

            logger.info(f"Job {job_id} completed steps: {completed_steps}")

            # If all required steps completed, aggregate results
            if self.required_steps.issubset(completed_steps):
                self.finalize_job(job_id)

        except Exception as e:
            logger.error(f"Error checking job completion: {e}")

    def finalize_job(self, job_id: str):
        """Aggregate results and mark job as completed"""
        try:
            logger.info(f"Finalizing job: {job_id}")

            # Get all individual results from Redis
            text = self.redis_client.get(f"orchestrator:job:{job_id}:text") or ""

            embeddings_json = self.redis_client.get(f"orchestrator:job:{job_id}:embeddings")
            embeddings = json.loads(embeddings_json) if embeddings_json else []

            entities_json = self.redis_client.get(f"orchestrator:job:{job_id}:entities")
            entities = json.loads(entities_json) if entities_json else []

            metadata_json = self.redis_client.get(f"orchestrator:job:{job_id}:metadata")
            metadata = json.loads(metadata_json) if metadata_json else {}

            # Aggregate into JobResults structure
            results = {
                "text": text,
                "embeddings": embeddings,
                "entities": entities,
                "metadata": metadata
            }

            # Store aggregated results
            self.redis_client.set(
                f"orchestrator:job:{job_id}:results",
                json.dumps(results)
            )

            # Set completion timestamp
            self.redis_client.hset(
                f"orchestrator:job:{job_id}:meta",
                "completed_at",
                int(time.time())
            )

            # Update final status to completed
            self.redis_client.hset(
                f"orchestrator:job:{job_id}:status",
                "status",
                "completed"
            )

            # Publish completion event
            self.event_bus.publish_job_completed(job_id)

            logger.info(f"Job {job_id} finalized and marked as completed")

        except Exception as e:
            logger.error(f"Error finalizing job: {e}")
            # Mark as failed
            self.redis_client.hset(
                f"orchestrator:job:{job_id}:status",
                "status",
                "failed"
            )
            self.redis_client.set(
                f"orchestrator:job:{job_id}:error",
                f"Finalization error: {str(e)}"
            )
            self.event_bus.publish_job_failed(job_id, str(e))

    def handle_event(self, message):
        """Handle Redis Pub/Sub events"""
        try:
            if message["type"] != "message":
                return

            event = json.loads(message["data"])
            event_type = event.get("event_type")
            job_id = event.get("job_id")

            logger.info(f"Received event: {event_type} for job {job_id}")

            # On progress event, check if job is complete
            if event_type == "job_progress" and job_id:
                self.check_job_completion(job_id)

        except Exception as e:
            logger.error(f"Error handling event: {e}")

    def start(self):
        """Start listening for job events"""
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
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    main()
