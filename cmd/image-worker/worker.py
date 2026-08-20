import asyncio
import json
import os
from typing import Dict, Any

import aio_pika

from pkg.image_client.client import MultimodalLLMClientPool
from pkg.worker_common.artifact_store import STORE
from pkg.worker_common.async_base import BaseAsyncWorker
from pkg.worker_common.chunking import chunk_text
from pkg.worker_common.security import validate_upload_path

QUEUE_NAME = os.getenv("IMAGE_QUEUE", "image")
METRICS_PORT = int(os.getenv("METRICS_PORT", "8006"))
UPLOAD_PATH = os.getenv("UPLOAD_PATH", "/app/data/uploads")


class ImageWorker(BaseAsyncWorker):
    def __init__(self):
        super().__init__(
            worker_name="image-worker",
            queue_name=QUEUE_NAME,
            metrics_port=METRICS_PORT,
        )
        self.llm_pool = MultimodalLLMClientPool()

    async def process_message(self, message: Dict[str, Any]) -> None:
        job_id = message.get("job_id")

        try:
            self.redis_client.hset(
                f"orchestrator:job:{job_id}:status", "status", "analyzing_image"
            )

            document_path = validate_upload_path(message["document_path"], UPLOAD_PATH)
            with open(document_path, "rb") as f:
                image_bytes = f.read()

            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: self.llm_pool.analyze(
                    image_bytes=image_bytes,
                    filename=message.get("filename", "image"),
                ),
            )

            text = result.extracted_text
            if result.description:
                text = f"{text}\n\n{result.description}"

            chunks = chunk_text(text)

            text_ref = STORE.put(text.encode("utf-8"))
            self.redis_client.set(f"orchestrator:job:{job_id}:text", text_ref)
            chunks_ref = STORE.put(json.dumps(chunks).encode("utf-8"))
            self.redis_client.set(f"orchestrator:job:{job_id}:chunks", chunks_ref)

            image_metadata = {
                "language": result.language,
                "description": result.description,
            }
            self.redis_client.set(
                f"orchestrator:job:{job_id}:metadata:image",
                json.dumps(image_metadata),
            )

            self.redis_client.hset(
                f"orchestrator:job:{job_id}:steps", "image", "completed"
            )

            job_message = {
                "job_id": job_id,
                "chunks": chunks,
                "document_metadata": image_metadata,
            }
            if message.get("entity_types"):
                job_message["entity_types"] = message["entity_types"]
            features = message.get("features") or []
            if features:
                job_message["features"] = features

            target_queues = ["embeddings", "entities", "metadata"]
            if "inferences" in features:
                target_queues.append("inferences")

            job_message_json = json.dumps(job_message).encode()
            for queue_name in target_queues:
                await self._channel.default_exchange.publish(
                    aio_pika.Message(
                        body=job_message_json,
                        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                        content_type="application/json",
                    ),
                    routing_key=queue_name,
                )

            self.event_bus.publish_job_progress(job_id, 25, "processing")
            self.jobs_total.labels(status="completed").inc()

        except Exception as e:
            if job_id:
                self.redis_client.hset(
                    f"orchestrator:job:{job_id}:status", "status", "failed"
                )
                self.redis_client.set(f"orchestrator:job:{job_id}:error", str(e))
                self.event_bus.publish_job_failed(job_id, str(e))
                self.jobs_total.labels(status="failed").inc()
            raise


def main():
    worker = ImageWorker()
    worker.run()


if __name__ == "__main__":
    asyncio.run(main())
