import asyncio
import json
import logging

from pydantic_settings import BaseSettings

from pkg.image_client.client import MultimodalLLMClientPool
from pkg.worker_common.async_base import BaseAsyncWorker
from pkg.worker_common.chunking import chunk_text
from pkg.worker_common.security import validate_upload_path

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    redis_url: str = "redis://localhost:6379"
    rabbitmq_url: str = "amqp://localhost:5672/"
    queue_name: str = "image"
    metrics_port: int = 8006
    prefetch_count: int = 2
    upload_path: str = "/app/data/uploads"

    class Config:
        env_prefix = "IMAGE_"


class ImageWorker(BaseAsyncWorker):
    def __init__(self):
        super().__init__(
            worker_name="image-worker",
            queue_name="image",
            metrics_port=Settings().metrics_port,
        )
        self.settings = Settings()
        self.llm_pool = MultimodalLLMClientPool()

    async def _process_message_async(self, message):
        job_id = None
        async with message.process(requeue=False):
            try:
                body = json.loads(message.body)
                job_id = body.get("job_id")

                self.redis_client.hset(
                    f"orchestrator:job:{job_id}:status", "status", "analyzing_image"
                )

                document_path = validate_upload_path(
                    body["document_path"], self.settings.upload_path
                )
                with open(document_path, "rb") as f:
                    image_bytes = f.read()

                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None,
                    lambda: self.llm_pool.analyze(
                        image_bytes=image_bytes,
                        filename=body.get("filename", "image"),
                    ),
                )

                text = result.extracted_text
                if result.description:
                    text = f"{text}\n\n{result.description}"

                chunks = chunk_text(text)

                self.redis_client.set(f"orchestrator:job:{job_id}:text", text)
                self.redis_client.set(
                    f"orchestrator:job:{job_id}:chunks", json.dumps(chunks)
                )

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
                if body.get("entity_types"):
                    job_message["entity_types"] = body["entity_types"]
                features = body.get("features") or []
                if features:
                    job_message["features"] = features

                target_queues = ["embeddings", "entities", "metadata"]
                if "inferences" in features:
                    target_queues.append("inferences")

                job_message_json = json.dumps(job_message).encode()
                for queue_name in target_queues:
                    await self._channel.default_exchange.publish(
                        self._make_message(job_message_json),
                        routing_key=queue_name,
                    )

                self.event_bus.publish_job_progress(job_id, 25, "processing")
                self.jobs_completed += 1

            except Exception as e:
                if job_id:
                    self.redis_client.hset(
                        f"orchestrator:job:{job_id}:status", "status", "failed"
                    )
                    self.redis_client.set(
                        f"orchestrator:job:{job_id}:error", str(e)
                    )
                    self.event_bus.publish_job_failed(job_id, str(e))
                    self.jobs_failed += 1
                raise


if __name__ == "__main__":
    ImageWorker().start()
