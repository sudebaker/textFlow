import asyncio
import json
import logging

from pydantic_settings import BaseSettings

from pkg.audio_client.client import WhisperClientPool
from pkg.worker_common.async_base import BaseAsyncWorker
from pkg.worker_common.security import validate_upload_path
from segment_chunker import SegmentChunker

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    redis_url: str = "redis://localhost:6379"
    rabbitmq_url: str = "amqp://localhost:5672/"
    queue_name: str = "audio"
    metrics_port: int = 8005
    max_audio_size_mb: int = 500
    prefetch_count: int = 2
    upload_path: str = "/app/data/uploads"
    whisper_urls: str = "http://whisper:9666"
    whisper_timeout: int = 300
    whisper_max_retries: int = 3

    class Config:
        env_prefix = "AUDIO_"


class AudioWorker(BaseAsyncWorker):
    def __init__(self):
        super().__init__(
            worker_name="audio-worker",
            queue_name="audio",
            metrics_port=Settings().metrics_port,
        )
        self.settings = Settings()
        self.whisper_pool = WhisperClientPool()
        self.chunker = SegmentChunker()

    async def _process_message_async(self, message):
        job_id = None
        async with message.process(requeue=False):
            try:
                body = json.loads(message.body)
                job_id = body.get("job_id")

                self.redis_client.hset(
                    f"orchestrator:job:{job_id}:status", "status", "transcribing"
                )

                document_path = body["document_path"]
                document_path = validate_upload_path(
                    document_path, self.settings.upload_path
                )
                with open(document_path, "rb") as f:
                    audio_bytes = f.read()

                size_mb = len(audio_bytes) / (1024 * 1024)
                if size_mb > self.settings.max_audio_size_mb:
                    raise ValueError(
                        f"Audio file size {size_mb:.1f}MB exceeds "
                        f"limit of {self.settings.max_audio_size_mb}MB"
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
    AudioWorker().start()
