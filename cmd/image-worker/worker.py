import asyncio
import json
import logging
import os

import aio_pika
import redis
from prometheus_client import Counter, Histogram, start_http_server

from pkg.events_python import EventBus
from pkg.image_client.client import MultimodalLLMClientPool
from pkg.logging_python import setup_logging
from pkg.worker_common.security import register_signal_handlers, validate_upload_path

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://localhost:5672/")
QUEUE_NAME = os.getenv("IMAGE_QUEUE", "image")
METRICS_PORT = int(os.getenv("METRICS_PORT", "8006"))
PREFETCH_COUNT = int(os.getenv("PREFETCH_COUNT", "2"))
UPLOAD_PATH = os.getenv("UPLOAD_PATH", "/app/data/uploads")

IMAGE_JOBS_TOTAL = Counter(
    "image_jobs_total", "Total image processing jobs", ["status"]
)
IMAGE_PROCESSING_TIME = Histogram("image_processing_seconds", "Image processing time")


def chunk_text(text: str, max_chars: int = 1500) -> list[dict]:
    """Split text into character-based chunks."""
    chunks = []
    start = 0
    chunk_num = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        chunks.append(
            {
                "chunk_id": f"chunk_{chunk_num:03d}",
                "text": text[start:end],
                "start_offset": start,
                "end_offset": end,
                "token_count": (end - start) // 4,
            }
        )
        if end >= len(text):
            break
        start = end
        chunk_num += 1
    return chunks


class ImageWorker:
    """Async RabbitMQ consumer for image analysis pipeline.

    Analyzes images via external multimodal LLM service, builds chunks,
    stores text in Redis, and publishes to downstream queues.

    Uses aio_pika (async) because LLM calls can take up to 120s.
    """

    def __init__(self):
        self.redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        self.event_bus = EventBus(self.redis_client)
        self.llm_pool = MultimodalLLMClientPool()
        self._channel = None

    async def _process_message_async(
        self,
        message: aio_pika.abc.AbstractIncomingMessage,
    ) -> None:
        job_id = None
        async with message.process(requeue=False):
            try:
                body = json.loads(message.body)
                job_id = body.get("job_id")

                self.redis_client.hset(
                    f"orchestrator:job:{job_id}:status", "status", "analyzing_image"
                )

                document_path = validate_upload_path(body["document_path"], UPLOAD_PATH)
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

                # Determine target queues based on features
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
                IMAGE_JOBS_TOTAL.labels(status="completed").inc()

            except Exception as e:
                if job_id:
                    self.redis_client.hset(
                        f"orchestrator:job:{job_id}:status", "status", "failed"
                    )
                    self.redis_client.set(f"orchestrator:job:{job_id}:error", str(e))
                    self.event_bus.publish_job_failed(job_id, str(e))
                    IMAGE_JOBS_TOTAL.labels(status="failed").inc()
                raise

    async def connect(self) -> aio_pika.abc.AbstractConnection:
        connection = await aio_pika.connect_robust(RABBITMQ_URL)
        self._channel = await connection.channel()
        await self._channel.set_qos(prefetch_count=PREFETCH_COUNT)

        queue = await self._channel.declare_queue(QUEUE_NAME, durable=True)
        await queue.consume(self._process_message_async)

        return connection


async def main():
    setup_logging("image-worker")

    worker = ImageWorker()
    connection = await worker.connect()
    register_signal_handlers(connection)

    start_http_server(METRICS_PORT)
    logger.info(
        f"Image worker started on queue {QUEUE_NAME}, metrics on port {METRICS_PORT}"
    )

    await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
