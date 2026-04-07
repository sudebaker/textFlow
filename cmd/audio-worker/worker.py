import asyncio
import json
import logging
import os
import signal
from typing import Optional

import aio_pika
import redis
from prometheus_client import Counter, Histogram, start_http_server

from pkg.audio_client.client import WhisperClientPool
from pkg.audio_client.exceptions import WhisperServiceError
from pkg.events_python import EventBus
from pkg.logging_python import setup_logging
from segment_chunker import SegmentChunker

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://localhost:5672/")
QUEUE_NAME = os.getenv("AUDIO_QUEUE", "audio")
METRICS_PORT = int(os.getenv("METRICS_PORT", "8005"))
MAX_AUDIO_SIZE_MB = int(os.getenv("MAX_AUDIO_SIZE_MB", "500"))
PREFETCH_COUNT = int(os.getenv("PREFETCH_COUNT", "2"))
UPLOAD_PATH = os.getenv("UPLOAD_PATH", "/app/data/uploads")

AUDIO_JOBS_TOTAL = Counter("audio_jobs_total", "Total audio processing jobs", ["status"])
AUDIO_PROCESSING_TIME = Histogram("audio_processing_seconds", "Audio processing time")


def validate_upload_path(file_path: str, allowed_dir: str) -> str:
    """Validate that file path is within allowed directory to prevent path traversal."""
    abs_allowed = os.path.abspath(allowed_dir)
    abs_file = os.path.abspath(file_path)
    
    if not abs_file.startswith(abs_allowed + os.sep) and abs_file != abs_allowed:
        raise ValueError(f"Invalid file path: {file_path} is not within {allowed_dir}")
    
    return abs_file


class AudioWorker:
    """Async RabbitMQ consumer for audio transcription pipeline.

    Transcribes audio via external Whisper service, builds chunks,
    stores text in Redis, and publishes to downstream queues.

    Uses aio_pika (async) because Whisper calls can take up to 300s.
    Blocking a pika synchronous consumer for that duration is unacceptable.
    """

    def __init__(self):
        self.redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        self.event_bus = EventBus(self.redis_client)
        self.whisper_pool = WhisperClientPool()
        self.chunker = SegmentChunker()

    async def _process_message_async(
        self,
        message: aio_pika.abc.AbstractIncomingMessage,
        channel: aio_pika.abc.AbstractChannel,
    ) -> None:
        job_id = None
        async with message.process(requeue=False):
            try:
                body = json.loads(message.body)
                job_id = body.get("job_id")

                self.redis_client.hset(
                    f"orchestrator:job:{job_id}:status", "status", "transcribing"
                )

                document_path = body["document_path"]
                document_path = validate_upload_path(document_path, UPLOAD_PATH)
                with open(document_path, "rb") as f:
                    audio_bytes = f.read()

                size_mb = len(audio_bytes) / (1024 * 1024)
                if size_mb > MAX_AUDIO_SIZE_MB:
                    raise ValueError(
                        f"Audio file size {size_mb:.1f}MB exceeds limit of {MAX_AUDIO_SIZE_MB}MB"
                    )

                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None,
                    lambda: self.whisper_pool.transcribe(
                        audio_bytes=audio_bytes,
                        filename=body.get("filename", "audio"),
                        language=body.get("language"),
                        diarize=body.get("diarize", False),
                    ),
                )

                text = result.text
                chunks = self.chunker.chunk(result)

                self.redis_client.set(f"orchestrator:job:{job_id}:text", text)
                self.redis_client.set(
                    f"orchestrator:job:{job_id}:chunks", json.dumps(chunks)
                )

                audio_metadata = {
                    "language": result.language,
                    "duration_seconds": result.duration_seconds,
                    "has_diarization": bool(result.segments),
                    "segment_count": len(result.segments) if result.segments else 0,
                }
                self.redis_client.set(
                    f"orchestrator:job:{job_id}:metadata:audio",
                    json.dumps(audio_metadata),
                )

                if result.segments:
                    self.redis_client.set(
                        f"orchestrator:job:{job_id}:audio_segments",
                        json.dumps([s.__dict__ for s in result.segments]),
                    )

                self.redis_client.hset(
                    f"orchestrator:job:{job_id}:steps", "audio", "completed"
                )

                job_message = {
                    "job_id": job_id,
                    "chunks": chunks,
                    "document_metadata": audio_metadata,
                }
                if body.get("entity_types"):
                    job_message["entity_types"] = body["entity_types"]

                job_message_json = json.dumps(job_message).encode()
                for queue_name in ["embeddings", "entities", "metadata"]:
                    await channel.default_exchange.publish(
                        aio_pika.Message(
                            body=job_message_json,
                            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                            content_type="application/json",
                        ),
                        routing_key=queue_name,
                    )

                self.event_bus.publish_job_progress(job_id, 25, "processing")
                AUDIO_JOBS_TOTAL.labels(status="completed").inc()

            except Exception as e:
                if job_id:
                    self.redis_client.hset(
                        f"orchestrator:job:{job_id}:status", "status", "failed"
                    )
                    self.redis_client.set(f"orchestrator:job:{job_id}:error", str(e))
                    self.event_bus.publish_job_failed(job_id, str(e))
                    AUDIO_JOBS_TOTAL.labels(status="failed").inc()
                raise

    async def connect(self) -> aio_pika.abc.AbstractConnection:
        connection = await aio_pika.connect_robust(RABBITMQ_URL)
        channel = await connection.channel()
        await channel.set_qos(prefetch_count=PREFETCH_COUNT)

        queue = await channel.declare_queue(QUEUE_NAME, durable=True)
        await queue.consume(self._process_message_async)

        return connection


async def main():
    setup_logging("audio-worker")

    worker = AudioWorker()
    connection = await worker.connect()

    def signal_handler(sig, frame):
        logger.info(f"Received signal {sig}, shutting down...")
        connection.close_loop()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    start_http_server(METRICS_PORT)
    logger.info(f"Audio worker started on queue {QUEUE_NAME}, metrics on port {METRICS_PORT}")

    await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())