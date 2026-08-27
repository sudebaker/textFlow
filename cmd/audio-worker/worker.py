import asyncio
import json
import os
from typing import Dict, Any

import aio_pika

from pkg.audio_client.client import WhisperClientPool
from pkg.audio_client.exceptions import WhisperServiceError
from pkg.worker_common.artifact_store import STORE
from pkg.worker_common.async_base import BaseAsyncWorker
from pkg.worker_common.security import validate_upload_path
from segment_chunker import SegmentChunker

QUEUE_NAME = os.getenv("AUDIO_QUEUE", "audio")
METRICS_PORT = int(os.getenv("METRICS_PORT", "8005"))
MAX_AUDIO_SIZE_MB = int(os.getenv("MAX_AUDIO_SIZE_MB", "500"))
UPLOAD_PATH = os.getenv("UPLOAD_PATH", "/app/data/uploads")


class AudioWorker(BaseAsyncWorker):
    def __init__(self):
        super().__init__(
            worker_name="audio-worker",
            queue_name=QUEUE_NAME,
            metrics_port=METRICS_PORT,
        )
        self.whisper_pool = WhisperClientPool()
        self.chunker = SegmentChunker()

    async def process_message(self, message: Dict[str, Any]) -> None:
        job_id = message.get("job_id")

        try:
            self.redis_client.hset(
                f"orchestrator:job:{job_id}:status", "status", "transcribing"
            )
            self.event_bus.publish_stage_event(job_id, "stage.started", "audio")

            document_path = message["document_path"]
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
                    filename=message.get("filename", "audio"),
                    language=message.get("language"),
                    diarize=message.get("diarize", False),
                ),
            )

            text = result.text
            chunks = self.chunker.chunk(result)

            text_ref = STORE.put(text.encode("utf-8"))
            self.redis_client.set(f"orchestrator:job:{job_id}:text", text_ref)
            chunks_ref = STORE.put(json.dumps(chunks).encode("utf-8"))
            self.redis_client.set(f"orchestrator:job:{job_id}:chunks", chunks_ref)

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
            self.event_bus.publish_stage_event(
                job_id,
                "stage.completed",
                "audio",
                metadata={"text_ref": text_ref, "chunks_ref": chunks_ref},
            )

            job_message = {
                "job_id": job_id,
                "document_metadata": audio_metadata,
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


def main() -> None:
    """Main entry point. Runs the async worker until a termination signal."""
    worker = AudioWorker()
    asyncio.run(worker.run())


if __name__ == "__main__":
    main()
