"""
Example workers using worker_common base classes.

Shows three patterns:
  1. BaseWorker         — synchronous RabbitMQ consumer (pika)
  2. BaseAsyncWorker    — async RabbitMQ consumer (aio_pika)
  3. BasePubSubWorker   — Redis pub/sub consumer

Usage:
    # Synchronous worker (most common)
    python example_worker.py

    # Async worker (extraction, audio, image)
    WORKER_MODE=async python example_worker.py

    # PubSub worker (completion)
    WORKER_MODE=pubsub python example_worker.py
"""

import json
import logging
import os
import sys
from typing import Dict

sys.path.insert(0, "/app")

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1. Synchronous worker (BaseWorker) — most Python workers
# ---------------------------------------------------------------------------
from pkg.worker_common.base import BaseWorker, ResourceManagerClient


class ExampleSyncWorker(BaseWorker):
    """Minimal sync worker consuming from RabbitMQ via pika.

    Also shows how to acquire/release a GPU via ResourceManagerClient and
    emit custom Prometheus metrics. BaseWorker already exposes:
      - self.jobs_total (Counter, labels=["status"])
      - self.job_duration (Histogram)
      - self.gpu_available (Gauge, labels=["device"])
    """

    def __init__(self):
        super().__init__(
            worker_name="example-sync-worker",
            queue_name=os.getenv("QUEUE_NAME", "example_queue"),
            metrics_port=int(os.getenv("METRICS_PORT", "8090")),
            requires_gpu=False,
        )
        # ResourceManagerClient is available via self.resource_manager in BaseWorker.
        # Direct instantiation is shown here for workers that only need this helper.
        self.resource_client = ResourceManagerClient(self.resource_manager_url)

    def process_message(self, message: Dict) -> Dict:
        job_id = message.get("job_id")
        logger.info(f"Processing job {job_id}")

        # --- Optional GPU resource lifecycle ---
        resource = None
        if self.requires_gpu:
            resource = self.resource_manager.acquire_resource(
                resource_type="gpu", worker_id=self.worker_name
            )
            if resource:
                logger.info(f"Acquired resource: {resource['resource_id']}")
                self.gpu_available.labels(device="cuda:0").set(1)
            else:
                self.gpu_available.labels(device="cuda:0").set(0)

        try:
            # --- Replace with actual work ---
            result = {"processed": True}

            # Update success metrics
            self.jobs_total.labels(status="success").inc()

            # Store result in Redis
            self.redis_client.set(
                f"orchestrator:job:{job_id}:result",
                json.dumps(result),
            )

            # Publish progress event
            self.event_bus.publish_job_progress(job_id, 100, "example_sync")

            logger.info(f"Job {job_id} done")
            return result

        finally:
            if resource:
                self.resource_manager.release_resource(resource["resource_id"])
                logger.info(f"Released resource: {resource['resource_id']}")


# ---------------------------------------------------------------------------
# 2. Async worker (BaseAsyncWorker) — extraction, audio, image
# ---------------------------------------------------------------------------
from pkg.worker_common.async_base import BaseAsyncWorker


class ExampleAsyncWorker(BaseAsyncWorker):
    """Minimal async worker consuming from RabbitMQ via aio_pika."""

    def __init__(self):
        super().__init__(
            worker_name="example-async-worker",
            queue_name=os.getenv("QUEUE_NAME", "example_async_queue"),
            metrics_port=int(os.getenv("METRICS_PORT", "8091")),
            requires_gpu=False,
        )

    async def process_message(self, message: Dict) -> None:
        job_id = message.get("job_id")
        logger.info(f"Async processing job {job_id}")

        # --- Replace with actual async work ---
        import asyncio
        await asyncio.sleep(0.1)

        self.redis_client.set(
            f"orchestrator:job:{job_id}:result",
            json.dumps({"processed": True}),
        )

        self.event_bus.publish_job_progress(job_id, 100, "example_async")
        logger.info(f"Async job {job_id} done")


# ---------------------------------------------------------------------------
# 3. PubSub worker (BasePubSubWorker) — completion
# ---------------------------------------------------------------------------
from pkg.worker_common.pubsub_base import BasePubSubWorker


class ExamplePubSubWorker(BasePubSubWorker):
    """Minimal pub/sub worker listening on Redis channel 'job:events'."""

    def __init__(self):
        super().__init__(
            worker_name="example-pubsub-worker",
            metrics_port=int(os.getenv("METRICS_PORT", "8092")),
        )

    def handle_event(self, message: Dict) -> None:
        channel = message.get("channel", b"").decode() if isinstance(message.get("channel"), bytes) else message.get("channel", "")
        data = message.get("data", b"").decode() if isinstance(message.get("data"), bytes) else message.get("data", "")

        if not data:
            return

        try:
            event = json.loads(data)
            job_id = event.get("job_id")
            logger.info(f"PubSub received event on {channel} for job {job_id}")

            # --- Replace with actual event handling ---

        except json.JSONDecodeError:
            logger.warning(f"Non-JSON message on {channel}: {data[:100]}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    mode = os.getenv("WORKER_MODE", "sync").lower()

    if mode == "async":
        import asyncio
        worker = ExampleAsyncWorker()
        asyncio.run(worker.run())
    elif mode == "pubsub":
        worker = ExamplePubSubWorker()
        worker.start()
    else:
        worker = ExampleSyncWorker()
        worker.run()
