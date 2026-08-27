"""
Event publishing module for Python workers.
Publishes job events to Redis Pub/Sub.
"""

import json
import time
from typing import Optional, Dict, Any
import redis


class EventBus:
    """Redis Pub/Sub event bus for job events."""

    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client

    def publish_event(
        self,
        job_id: str,
        event_type: str,
        progress: Optional[int] = None,
        status: Optional[str] = None,
        error: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Publish a job event to Redis Pub/Sub."""
        event = {
            "event_type": event_type,
            "job_id": job_id,
            "timestamp": time.time(),
        }

        if progress is not None:
            event["progress"] = progress
        if status is not None:
            event["status"] = status
        if error is not None:
            event["error"] = error
        if metadata is not None:
            event["metadata"] = metadata

        # Publish to general job events channel
        self.redis.publish("job:events", json.dumps(event))

        # Also publish to job-specific channel for SSE
        self.redis.publish(f"job:{job_id}:events", json.dumps(event))

    def publish_job_progress(self, job_id: str, progress: int, status: str) -> None:
        """Publish a job progress event."""
        self.publish_event(job_id, "job_progress", progress=progress, status=status)

    def publish_job_completed(
        self, job_id: str, metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """Publish a job completed event."""
        self.publish_event(
            job_id, "job_completed", progress=100, status="completed", metadata=metadata
        )

    def publish_job_failed(self, job_id: str, error: str) -> None:
        """Publish a job failed event."""
        self.publish_event(job_id, "job_failed", status="failed", error=error)

    def publish_job_inference_chunk_progress(
        self,
        job_id: str,
        chunks_done: int,
        chunks_total: int,
    ) -> None:
        """Publish incremental progress for each inference chunk completed."""
        # Scale from 60% (post-entities) to 79% (just before final assembly at 80%)
        base = 60
        span = 19
        progress = base + int(span * chunks_done / max(chunks_total, 1))
        self.publish_event(
            job_id,
            "job_progress",
            progress=progress,
            status="inferences",
            metadata={"chunks_done": chunks_done, "chunks_total": chunks_total},
        )

    def publish_stage_event(
        self,
        job_id: str,
        event_type: str,
        stage: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Publish a stage-level event (spec 4.5).

        event_type is one of: stage.queued, stage.started, stage.completed,
        stage.failed. The stage name is carried in metadata["stage"].
        """
        merged = {"stage": stage}
        if metadata:
            merged.update(metadata)
        self.publish_event(job_id, event_type, metadata=merged)
