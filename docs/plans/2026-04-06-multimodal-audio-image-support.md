# Multimodal Support: Audio and Image Processing

**Date:** 2026-04-06  
**Status:** Design approved, ready for implementation  
**Scope:** Add audio (via external Whisper service) and image (via external multimodal LLM service) to the existing document pipeline, producing text, chunks, embeddings, entities and metadata via the same downstream workers.

---

## Goals

Extend the orchestrator to accept audio files (`.mp3`, `.wav`, `.m4a`, `.ogg`) and image files (`.jpg`, `.jpeg`, `.png`) and route them through new, dedicated workers that:
- Call external HTTP services (Whisper for audio, multimodal LLM for images)
- Store extracted text in Redis under the same key schema (`orchestrator:job:{id}:text`)
- Forward the result to the existing `embeddings`, `entities`, and `metadata` queues
- Respect the air-gapped constraint: no model loading in this repo's workers

---

## Context

### Existing Pipeline (documents)

```
Client → POST /upload
       → orchestrator (Go, 8080)
       → RabbitMQ queue: extraction
       → extraction-worker (Python, Docling client)
       → Redis: job:{id}:text
       → RabbitMQ queues: embeddings | entities | metadata
       → [embeddings-worker, entities-worker, metadata-worker]
       → completion-worker
       → Redis: job:{id}:results
```

### Key Constraints

- **Air-gapped deployment**: workers do NOT load models at runtime. They are HTTP clients to external services. `pip install` is allowed at build time.
- **Two worker patterns exist in this project:**
  - `BaseWorker` (`pkg/worker_common/base.py`): synchronous, `pika`-based. Provides Redis, RabbitMQ, retry with exponential backoff via delayed exchange, Prometheus metrics, signal handling, health checks. The publish method is `_publish_to_queue(queue, message)`.
  - `ExtractionWorker` pattern (`cmd/extraction-worker/worker.py`): fully async, `aio_pika`-based. Used when operations involve long-running I/O (Docling polling up to 1800s) that would block the synchronous consumer thread.
- **Audio and image workers MUST use the async pattern** (same as `ExtractionWorker`) because Whisper calls can take up to 300s and multimodal LLM calls up to 120s — blocking the `pika` consumer thread for these durations is unacceptable.
- **Message format**: simple project-specific JSON (`job_id` always present).
- **Redis keys**: `orchestrator:job:{id}:{field}` (text, chunks, embeddings, entities, metadata, steps, status, results).
  - Status is written with `hset`: `redis_client.hset(f"orchestrator:job:{job_id}:status", "status", "transcribing")`
  - Steps are written with `hset`: `redis_client.hset(f"orchestrator:job:{job_id}:steps", "audio", "completed")`
- **Downstream message format**: the existing embeddings/entities/metadata workers expect `chunks` in the message body: `{"job_id": ..., "chunks": [...], "document_metadata": {...}}`.
- **External services** are not part of this repo. They expose HTTP endpoints. Our workers are clients.

---

## Architecture

### New Pipeline (audio and images)

```
Client → POST /upload (audio or image)
       → orchestrator detects content_type
       → RabbitMQ queue: audio | image
       → audio-worker OR image-worker (Python, external HTTP client)
       → Redis: job:{id}:text
       → RabbitMQ queues: embeddings | entities | metadata
       → [existing downstream workers — unchanged]
       → completion-worker
       → Redis: job:{id}:results
```

### Component Overview

```
┌──────────────────────────────────────────────────────────────┐
│  orchestrator (Go, 8080)                                     │
│  - Detects content type from file extension                  │
│  - Routes to audio / image / extraction queue                │
│  - New JobStatus: StatusTranscribing, StatusAnalyzingImage   │
└───────────────────────┬──────────────────────────────────────┘
                        │ RabbitMQ
          ┌─────────────┼──────────────┐
          │             │              │
    [extraction]     [audio]       [image]
          │             │              │
  extraction-     audio-worker  image-worker
  worker           (Python)       (Python)
  (unchanged)        │              │
                  Whisper        Multimodal
                  service         LLM
                  (external)    (external)
                      │              │
                      └──────┬───────┘
                             │ Redis: job:{id}:text + chunks
                             │ RabbitMQ: embeddings | entities | metadata
                    [existing downstream workers — unchanged]
```

---

## External Service Contracts

### Whisper Service (audio transcription)

**Request:**
```
POST /transcribe
Content-Type: multipart/form-data

file=<audio_bytes>   (field name: "file")
language=<str>       (optional, e.g. "es", "en")
diarize=<bool>       (optional, default false — enables speaker labels)
```

**Response (success):**
```json
{
  "text": "Full transcription...",
  "language": "es",
  "duration_seconds": 142.5,
  "segments": [
    {
      "start": 0.0,
      "end": 5.2,
      "text": "Hola, buenos días.",
      "speaker": "int1"
    },
    {
      "start": 5.3,
      "end": 10.1,
      "text": "Buenas, ¿en qué le puedo ayudar?",
      "speaker": "int2"
    }
  ]
}
```

**Notes:**
- `segments` is optional; present only when `diarize=true`.
- `speaker` values are `int1`, `int2`, etc. (interlocutor labels).
- When diarization is disabled, `text` is used directly.

### Multimodal LLM Service (image analysis)

**Request:**
```
POST /analyze
Content-Type: multipart/form-data

file=<image_bytes>   (field name: "file")
prompt=<str>         (optional — if not sent, service uses its default extraction prompt)
```

**Response (success):**
```json
{
  "extracted_text": "Text visible in the image...",
  "description": "A document showing a table of quarterly revenues...",
  "language": "es",
  "confidence": 0.94
}
```

**Notes:**
- `extracted_text` is the primary field stored in Redis.
- `description` is appended to `extracted_text` if present, separated by `\n\n`.
- `confidence` is logged but not stored.

---

## Data Contracts (Go)

### New types in `internal/models/job.go`

```go
// ContentType identifies the type of uploaded content.
type ContentType string

const (
    ContentTypeDocument ContentType = "document"
    ContentTypeAudio    ContentType = "audio"
    ContentTypeImage    ContentType = "image"
)

// AudioSegment represents a single timed segment with optional speaker label.
type AudioSegment struct {
    Start   float64 `json:"start"`
    End     float64 `json:"end"`
    Text    string  `json:"text"`
    Speaker string  `json:"speaker,omitempty"`
}

// AudioMetadata holds transcription-specific metadata stored in Redis.
type AudioMetadata struct {
    Language        string  `json:"language,omitempty"`
    DurationSeconds float64 `json:"duration_seconds,omitempty"`
    HasDiarization  bool    `json:"has_diarization"`
    SegmentCount    int     `json:"segment_count,omitempty"`
}
// Note: individual segments are NOT stored in AudioMetadata to avoid
// oversized Redis values on long recordings (thousands of segments).
// The full segments array is stored separately in Redis under
// orchestrator:job:{id}:audio_segments as a JSON array.

// ImageMetadata holds image analysis metadata stored in Redis.
type ImageMetadata struct {
    Language    string  `json:"language,omitempty"`
    Description string  `json:"description,omitempty"`
    Confidence  float64 `json:"confidence,omitempty"`
}
```

### New JobStatus values in `internal/models/job.go`

```go
const (
    // ... existing statuses ...
    StatusTranscribing   JobStatus = "transcribing"    // audio-worker processing
    StatusAnalyzingImage JobStatus = "analyzing_image" // image-worker processing
)
```

### Updated JobMessage in `internal/models/job.go`

`JobMessage` must include the new `ContentType` and `Diarize` fields:

```go
type JobMessage struct {
    // ... existing fields ...
    ContentType ContentType `json:"content_type,omitempty"`
    Diarize     bool        `json:"diarize,omitempty"`
}
```

### New RabbitMQ message fields

Both `audio` and `image` queue messages use the existing JSON structure with these additions:

```json
{
  "job_id": "uuid",
  "content_type": "audio",
  "document_path": "/uploads/uuid/audio.mp3",
  "filename": "audio.mp3",
  "mime_type": "audio/mpeg",
  "diarize": true
}
```

`diarize` is sent as a form field in `POST /upload` (e.g. `diarize=true`). It is ignored for non-audio content types.

---

## Client Pool Design (pkg/)

Both clients share the same pattern: a pool of URLs with thread-safe round-robin selection and failover.

### `pkg/audio_client/client.py`

```python
import threading

class WhisperClientPool:
    """HTTP client pool for Whisper transcription service.
    
    Reads WHISPER_URLS env var (comma-separated).
    Thread-safe round-robin selection with automatic failover to next URL on error.
    Retries up to MAX_RETRIES=3 with exponential backoff before failing.
    """

    def __init__(self):
        urls_env = os.getenv("WHISPER_URLS", "http://whisper:9000")
        self._urls = [u.strip() for u in urls_env.split(",") if u.strip()]
        self._index = 0
        self._lock = threading.Lock()  # thread-safe index access
        self._timeout = int(os.getenv("WHISPER_TIMEOUT", "300"))
        self._max_retries = int(os.getenv("WHISPER_MAX_RETRIES", "3"))

    def _next_url(self) -> str:
        with self._lock:
            url = self._urls[self._index % len(self._urls)]
            self._index = (self._index + 1) % len(self._urls)
        return url

    def transcribe(
        self,
        audio_bytes: bytes,
        filename: str,
        language: Optional[str] = None,
        diarize: bool = False,
    ) -> TranscriptionResult:
        ...
```

### `pkg/image_client/client.py`

```python
import threading

class MultimodalLLMClientPool:
    """HTTP client pool for multimodal LLM image analysis service.
    
    Reads MULTIMODAL_LLM_URLS env var (comma-separated).
    Thread-safe round-robin selection with automatic failover to next URL on error.
    Retries up to MAX_RETRIES=3 with exponential backoff before failing.
    """

    def __init__(self):
        urls_env = os.getenv("MULTIMODAL_LLM_URLS", "http://multimodal-llm:8000")
        self._urls = [u.strip() for u in urls_env.split(",") if u.strip()]
        self._index = 0
        self._lock = threading.Lock()
        self._timeout = int(os.getenv("MULTIMODAL_LLM_TIMEOUT", "120"))
        self._max_retries = int(os.getenv("MULTIMODAL_LLM_MAX_RETRIES", "3"))

    def _next_url(self) -> str:
        with self._lock:
            url = self._urls[self._index % len(self._urls)]
            self._index = (self._index + 1) % len(self._urls)
        return url

    def analyze(
        self,
        image_bytes: bytes,
        filename: str,
    ) -> ImageAnalysisResult:
        ...
```

### Shared exception base

`ServiceUnavailableError` is defined once in `pkg/shared/exceptions.py` and imported by both client packages:

```python
# pkg/shared/exceptions.py
class ServiceUnavailableError(Exception):
    """Raised when an external service is unreachable after all retries."""
```

### Failover and Retry Logic

```
Attempt 1 → URL[0]  → success → return result
Attempt 1 → URL[0]  → 5xx/timeout → backoff 1s → Attempt 2 → URL[1]
Attempt 2 → URL[1]  → 5xx/timeout → backoff 2s → Attempt 3 → URL[2 % len]
Attempt 3 → fail    → raise ServiceUnavailableError → job status = "failed"
```

- Backoff: `min(2 ** attempt, 30)` seconds.
- URL rotation: `_next_url()` advances `_index` per attempt (thread-safe).
- Pool instance is shared within one worker process (not across processes).

---

## Audio Chunking with Speaker Labels

When diarization is enabled, the audio-worker builds chunks from segments grouped by speaker turn:

```
[int1]: Hola, buenos días. ¿Cómo puedo ayudarle hoy?
[int2]: Buenos días. Tengo un problema con mi factura del mes pasado.
[int1]: Entendido. ¿Me puede dar su número de cliente?
```

Rules:
- Consecutive segments from the same speaker are merged into one chunk.
- Maximum chunk size: `AUDIO_CHUNK_MAX_CHARS` (default: 1500).
- If no diarization, standard token-based chunking (same as `chunk_text()` in `extraction-worker`) is applied to `result.text`.
- Chunks are stored in Redis: `orchestrator:job:{id}:chunks`.
- Each chunk dict matches the format produced by `chunk_text()`: `{"chunk_id": "chunk_000", "text": "...", "start_offset": 0, "end_offset": N, "token_count": N}`.

### Image Chunking

Images also produce chunks (required by downstream workers). Since image text is typically short, a single chunk is produced using the same `chunk_text()` helper. This ensures `embeddings-worker` receives the expected `chunks` field.

---

## Implementation Plan

> **IMPORTANT — Task ordering**: Tasks 1.2 and 1.4 must be done before 1.3. The config fields (`AudioQueue`, `ImageQueue`) and the model types (`ContentType`, `JobMessage`) must exist before the routing logic in the upload handler can reference them.

### Phase 1: Go orchestrator changes

**Task 1.1 — Add new file extensions to whitelist**  
File: `cmd/orchestrator/main.go`

Add to `allowedExtensions` map:
```go
".mp3": true,
".wav": true,
".m4a": true,
".ogg": true,
```
(`.jpg`, `.jpeg`, `.png` already added in previous plan.)

Update error message to include audio types. Add `MAX_AUDIO_SIZE_MB` constant (default: 500) and enforce it alongside the existing file size check.

**Task 1.2 — Add ContentType to models and routing logic**  
File: `internal/models/job.go`

Add `ContentType`, `AudioMetadata`, `ImageMetadata`, `AudioSegment` types and new `JobStatus` constants (see "Data Contracts" section above). Update `JobMessage` struct to include `ContentType ContentType` and `Diarize bool` fields.

**Task 1.3 — Add new queue names to config**  
File: `internal/config/config.go`

```go
AudioQueue string `env:"AUDIO_QUEUE" default:"audio"`
ImageQueue string `env:"IMAGE_QUEUE" default:"image"`
```

**Task 1.4 — Route by content type in upload handler**  
File: `cmd/orchestrator/main.go`

After extension validation, detect `diarize` form field and content type, then route:
```go
diarize := c.PostForm("diarize") == "true"

var targetQueue string
switch ext {
case ".mp3", ".wav", ".m4a", ".ogg":
    jobMsg.ContentType = models.ContentTypeAudio
    jobMsg.Diarize = diarize
    targetQueue = cfg.AudioQueue        // "audio"
case ".jpg", ".jpeg", ".png":
    jobMsg.ContentType = models.ContentTypeImage
    targetQueue = cfg.ImageQueue        // "image"
default:
    jobMsg.ContentType = models.ContentTypeDocument
    targetQueue = cfg.ExtractionQueue   // "extraction"
}
```

**Task 1.5 — Update Swagger docs**  
File: `docs/swagger/docs.go` (and `docs/swagger/swagger.yaml` if present)

- Add `.mp3`, `.wav`, `.m4a`, `.ogg` to accepted file types in `POST /upload` docs.
- Document the `diarize` form field (boolean, optional, audio only).
- Add `transcribing` and `analyzing_image` to the `JobStatus` enum.

**Task 1.6 — Build and verify**
```bash
make build-orchestrator
make test
```

---

### Phase 2: Shared exceptions + Python client libraries

**Task 2.1 — Create `pkg/shared/exceptions.py`**

```python
class ServiceUnavailableError(Exception):
    """Raised when an external service is unreachable after all retries."""
```

**Task 2.2 — Create `pkg/audio_client/`**

Files to create:
- `pkg/audio_client/__init__.py`
- `pkg/audio_client/client.py` — `WhisperClientPool` (see Client Pool Design above)
- `pkg/audio_client/models.py` — `TranscriptionResult`, `AudioSegment`
- `pkg/audio_client/exceptions.py` — `WhisperServiceError`; imports `ServiceUnavailableError` from `pkg.shared.exceptions`
- `pkg/audio_client/tests/__init__.py`
- `pkg/audio_client/tests/test_client.py`

Key env vars read by `WhisperClientPool`:
```
WHISPER_URLS=http://whisper-1:9000,http://whisper-2:9000
WHISPER_TIMEOUT=300
WHISPER_MAX_RETRIES=3
```

**Task 2.3 — Create `pkg/image_client/`**

Files to create:
- `pkg/image_client/__init__.py`
- `pkg/image_client/client.py` — `MultimodalLLMClientPool`
- `pkg/image_client/models.py` — `ImageAnalysisResult`
- `pkg/image_client/exceptions.py` — `MultimodalLLMServiceError`; imports `ServiceUnavailableError` from `pkg.shared.exceptions`
- `pkg/image_client/tests/__init__.py`
- `pkg/image_client/tests/test_client.py`

Key env vars read by `MultimodalLLMClientPool`:
```
MULTIMODAL_LLM_URLS=http://llm-1:8000,http://llm-2:8000
MULTIMODAL_LLM_TIMEOUT=120
MULTIMODAL_LLM_MAX_RETRIES=3
```

**Task 2.4 — Verify with unit tests**
```bash
pytest pkg/audio_client/tests/ -v
pytest pkg/image_client/tests/ -v
```

---

### Phase 3: audio-worker

**Task 3.1 — Create worker structure**

Files to create:
```
cmd/audio-worker/
├── worker.py                  # Async worker (aio_pika pattern, NOT BaseWorker)
├── segment_chunker.py         # Diarization-aware chunker
├── requirements.txt
├── Dockerfile
└── app/
    └── config/
        └── settings.py        # Pydantic BaseSettings
```

**Task 3.2 — Implement `worker.py` (async pattern)**

Follow `cmd/extraction-worker/worker.py` as the reference. Key differences from BaseWorker:

```python
import asyncio
import json
import os
import signal
import time
from typing import Optional

import aio_pika
import redis
from prometheus_client import Counter, Histogram, start_http_server

from pkg.audio_client.client import WhisperClientPool
from pkg.audio_client.exceptions import WhisperServiceError
from pkg.shared.exceptions import ServiceUnavailableError
from pkg.events_python import EventBus
from pkg.worker_common.rabbitmq_async import declare_queue_async
from segment_chunker import SegmentChunker

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://localhost:5672/")
QUEUE_NAME = os.getenv("AUDIO_QUEUE", "audio")
METRICS_PORT = int(os.getenv("METRICS_PORT", "8005"))
MAX_AUDIO_SIZE_MB = int(os.getenv("MAX_AUDIO_SIZE_MB", "500"))
PREFETCH_COUNT = int(os.getenv("PREFETCH_COUNT", "2"))

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

                # Update status
                self.redis_client.hset(
                    f"orchestrator:job:{job_id}:status", "status", "transcribing"
                )

                # Read audio file from disk
                document_path = body["document_path"]
                with open(document_path, "rb") as f:
                    audio_bytes = f.read()

                # Enforce size limit
                size_mb = len(audio_bytes) / (1024 * 1024)
                if size_mb > MAX_AUDIO_SIZE_MB:
                    raise ValueError(
                        f"Audio file size {size_mb:.1f}MB exceeds limit of {MAX_AUDIO_SIZE_MB}MB"
                    )

                # Call Whisper (blocking in thread pool to avoid blocking event loop)
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

                # Build text and chunks
                text = result.text
                chunks = self.chunker.chunk(result)

                # Store in Redis
                self.redis_client.set(f"orchestrator:job:{job_id}:text", text)
                self.redis_client.set(
                    f"orchestrator:job:{job_id}:chunks", json.dumps(chunks)
                )

                # Store audio metadata (without full segments array)
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

                # Store full segments separately (only if diarized)
                if result.segments:
                    self.redis_client.set(
                        f"orchestrator:job:{job_id}:audio_segments",
                        json.dumps([s.__dict__ for s in result.segments]),
                    )

                # Mark step complete
                self.redis_client.hset(
                    f"orchestrator:job:{job_id}:steps", "audio", "completed"
                )

                # Publish to downstream queues
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

            except Exception as e:
                if job_id:
                    self.redis_client.hset(
                        f"orchestrator:job:{job_id}:status", "status", "failed"
                    )
                    self.redis_client.set(f"orchestrator:job:{job_id}:error", str(e))
                    self.event_bus.publish_job_failed(job_id, str(e))
                raise
```

**Task 3.3 — Implement `segment_chunker.py`**

```python
from typing import List, Optional
from pkg.audio_client.models import AudioSegment, TranscriptionResult

CHARS_PER_TOKEN = 4


def _simple_chunk(text: str, max_chars: int) -> List[dict]:
    """Split text into character-based chunks mirroring chunk_text() format."""
    chunks = []
    start = 0
    chunk_num = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        chunks.append({
            "chunk_id": f"chunk_{chunk_num:03d}",
            "text": text[start:end],
            "start_offset": start,
            "end_offset": end,
            "token_count": (end - start) // CHARS_PER_TOKEN,
        })
        if end >= len(text):
            break
        start = end
        chunk_num += 1
    return chunks


class SegmentChunker:
    """Groups diarized segments by speaker turn into text chunks.
    
    Output format matches chunk_text() in extraction-worker so downstream
    workers receive identical chunk structures regardless of content type.
    """

    def __init__(self, max_chars: int = 1500):
        self.max_chars = max_chars

    def chunk(self, result: TranscriptionResult) -> List[dict]:
        if not result.segments:
            return _simple_chunk(result.text, self.max_chars)
        return self._chunk_by_speaker(result.segments)

    def _chunk_by_speaker(self, segments: List[AudioSegment]) -> List[dict]:
        """Merge consecutive same-speaker segments; split on max_chars."""
        chunks = []
        chunk_num = 0
        buffer_speaker: Optional[str] = None
        buffer_texts: List[str] = []
        buffer_start = 0

        def _flush(end_offset: int) -> None:
            nonlocal chunk_num
            if not buffer_texts:
                return
            combined = " ".join(buffer_texts)
            prefix = f"[{buffer_speaker}]: " if buffer_speaker else ""
            full_text = f"{prefix}{combined}"
            chunks.append({
                "chunk_id": f"chunk_{chunk_num:03d}",
                "text": full_text,
                "start_offset": buffer_start,
                "end_offset": end_offset,
                "token_count": len(full_text) // CHARS_PER_TOKEN,
            })
            chunk_num += 1

        for i, seg in enumerate(segments):
            if seg.speaker != buffer_speaker or (
                sum(len(t) for t in buffer_texts) + len(seg.text) > self.max_chars
            ):
                _flush(i)
                buffer_speaker = seg.speaker
                buffer_texts = [seg.text]
                buffer_start = i
            else:
                buffer_texts.append(seg.text)

        _flush(len(segments))
        return chunks
```

**Task 3.4 — Implement Dockerfile**

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install dependencies at build time (pip install is allowed in air-gapped builds)
COPY cmd/audio-worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy shared packages
COPY pkg/worker_common/ /app/pkg/worker_common/
COPY pkg/audio_client/ /app/pkg/audio_client/
COPY pkg/shared/ /app/pkg/shared/
COPY pkg/events_python.py /app/pkg/events_python.py
COPY pkg/logging_python.py /app/pkg/logging_python.py

# Copy worker source
COPY cmd/audio-worker/ /app/

# No HF_HUB_OFFLINE or TRANSFORMERS_OFFLINE needed — this worker loads no local models
CMD ["python", "worker.py"]
```

**Task 3.5 — Write `requirements.txt`**

```
aio-pika>=9.0.0
redis>=5.0.0
requests>=2.31.0
prometheus-client>=0.19.0
pydantic-settings>=2.0.0
fastapi>=0.104.0
uvicorn>=0.24.0
```

**Task 3.6 — Syntax check**
```bash
python -m py_compile cmd/audio-worker/worker.py cmd/audio-worker/segment_chunker.py
```

---

### Phase 4: image-worker

**Task 4.1 — Create worker structure**

Files to create:
```
cmd/image-worker/
├── worker.py                  # Async worker (aio_pika pattern, NOT BaseWorker)
├── requirements.txt
├── Dockerfile
└── app/
    └── config/
        └── settings.py
```

**Task 4.2 — Implement `worker.py` (async pattern)**

```python
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
                    f"orchestrator:job:{job_id}:status", "status", "analyzing_image"
                )

                with open(body["document_path"], "rb") as f:
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

                # Chunk text using the same helper as extraction-worker
                chunks = chunk_text(text)

                self.redis_client.set(f"orchestrator:job:{job_id}:text", text)
                self.redis_client.set(
                    f"orchestrator:job:{job_id}:chunks", json.dumps(chunks)
                )

                image_metadata = {
                    "language": result.language,
                    "description": result.description,
                    # confidence intentionally omitted from storage (log only)
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

            except Exception as e:
                if job_id:
                    self.redis_client.hset(
                        f"orchestrator:job:{job_id}:status", "status", "failed"
                    )
                    self.redis_client.set(f"orchestrator:job:{job_id}:error", str(e))
                    self.event_bus.publish_job_failed(job_id, str(e))
                raise
```

**Task 4.3 — Implement Dockerfile**

```dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY cmd/image-worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pkg/worker_common/ /app/pkg/worker_common/
COPY pkg/image_client/ /app/pkg/image_client/
COPY pkg/shared/ /app/pkg/shared/
COPY pkg/events_python.py /app/pkg/events_python.py
COPY pkg/logging_python.py /app/pkg/logging_python.py

COPY cmd/image-worker/ /app/

CMD ["python", "worker.py"]
```

**Task 4.4 — Write `requirements.txt`**

Same as audio-worker.

**Task 4.5 — Syntax check**
```bash
python -m py_compile cmd/image-worker/worker.py
```

---

### Phase 5: Infrastructure wiring

**Task 5.1 — Update `deploy/docker/docker-compose.yml`**

Add two new services (do NOT remove or modify `extraction-worker`):

```yaml
audio-worker:
  build:
    context: ../..
    dockerfile: cmd/audio-worker/Dockerfile
  networks:
    - backend
    - datastore
  environment:
    - RABBITMQ_URL=${RABBITMQ_URL}
    - REDIS_URL=${REDIS_URL}
    - WHISPER_URLS=${WHISPER_URLS}
    - WHISPER_TIMEOUT=${WHISPER_TIMEOUT:-300}
    - WHISPER_MAX_RETRIES=${WHISPER_MAX_RETRIES:-3}
    - AUDIO_QUEUE=audio
    - AUDIO_CHUNK_MAX_CHARS=${AUDIO_CHUNK_MAX_CHARS:-1500}
    - MAX_AUDIO_SIZE_MB=${MAX_AUDIO_SIZE_MB:-500}
    - METRICS_PORT=8005
  restart: unless-stopped
  depends_on:
    - rabbitmq
    - redis

image-worker:
  build:
    context: ../..
    dockerfile: cmd/image-worker/Dockerfile
  networks:
    - backend
    - datastore
  environment:
    - RABBITMQ_URL=${RABBITMQ_URL}
    - REDIS_URL=${REDIS_URL}
    - MULTIMODAL_LLM_URLS=${MULTIMODAL_LLM_URLS}
    - MULTIMODAL_LLM_TIMEOUT=${MULTIMODAL_LLM_TIMEOUT:-120}
    - MULTIMODAL_LLM_MAX_RETRIES=${MULTIMODAL_LLM_MAX_RETRIES:-3}
    - IMAGE_QUEUE=image
    - METRICS_PORT=8006
  restart: unless-stopped
  depends_on:
    - rabbitmq
    - redis
```

**Task 5.2 — Update `.env.example`**

```bash
# Audio Worker
WHISPER_URLS=http://whisper:9000
WHISPER_TIMEOUT=300
WHISPER_MAX_RETRIES=3
AUDIO_CHUNK_MAX_CHARS=1500
MAX_AUDIO_SIZE_MB=500

# Image Worker
MULTIMODAL_LLM_URLS=http://multimodal-llm:8000
MULTIMODAL_LLM_TIMEOUT=120
MULTIMODAL_LLM_MAX_RETRIES=3

# Queue names (defaults match orchestrator defaults)
AUDIO_QUEUE=audio
IMAGE_QUEUE=image
```

**Task 5.3 — Update Makefile**

Add new targets; preserve existing `run-extraction-worker` in `run-workers`:

```makefile
run-audio-worker:
	@cd cmd/audio-worker && python worker.py

run-image-worker:
	@cd cmd/image-worker && python worker.py

# run-workers: includes ALL python workers including extraction-worker (unchanged)
run-workers: run-embeddings-worker run-entities-worker run-extraction-worker run-audio-worker run-image-worker
```

**Task 5.4 — Verify docker-compose syntax**
```bash
docker compose -f deploy/docker/docker-compose.yml config > /dev/null
```

---

### Phase 6: Tests

**Task 6.1 — Unit tests for `pkg/audio_client/`**

File: `pkg/audio_client/tests/test_client.py`

- Test successful transcription response parsing.
- Test failover: first URL returns 500, second URL succeeds.
- Test max retries exceeded → `ServiceUnavailableError`.
- Test round-robin URL selection advances `_index` correctly.
- Test thread-safe index access (two threads call `_next_url()` concurrently).

**Task 6.2 — Unit tests for `pkg/image_client/`**

File: `pkg/image_client/tests/test_client.py`

- Same structure as audio client tests.
- Test `description` appended to `extracted_text` when present.
- Test `confidence` is returned in result but not passed to Redis.

**Task 6.3 — Unit tests for `cmd/audio-worker/`**

File: `cmd/audio-worker/tests/test_segment_chunker.py`

- Test chunking with diarized segments grouped by speaker.
- Test max char limit splits a single speaker turn into multiple chunks.
- Test fallback to `_simple_chunk()` when no segments.
- Test output format matches `chunk_text()` structure (`chunk_id`, `text`, `start_offset`, `end_offset`, `token_count`).

**Task 6.4 — Run all tests**
```bash
make test
make test-python
```

---

## Files to Create

| File | Purpose |
|------|---------|
| `pkg/shared/__init__.py` | Package init |
| `pkg/shared/exceptions.py` | `ServiceUnavailableError` (shared base) |
| `pkg/audio_client/__init__.py` | Package init |
| `pkg/audio_client/client.py` | `WhisperClientPool` |
| `pkg/audio_client/models.py` | `TranscriptionResult`, `AudioSegment` |
| `pkg/audio_client/exceptions.py` | `WhisperServiceError` |
| `pkg/audio_client/tests/__init__.py` | Test package init |
| `pkg/audio_client/tests/test_client.py` | Unit tests |
| `pkg/image_client/__init__.py` | Package init |
| `pkg/image_client/client.py` | `MultimodalLLMClientPool` |
| `pkg/image_client/models.py` | `ImageAnalysisResult` |
| `pkg/image_client/exceptions.py` | `MultimodalLLMServiceError` |
| `pkg/image_client/tests/__init__.py` | Test package init |
| `pkg/image_client/tests/test_client.py` | Unit tests |
| `cmd/audio-worker/worker.py` | Main audio worker (async, aio_pika) |
| `cmd/audio-worker/segment_chunker.py` | Speaker-aware chunker |
| `cmd/audio-worker/requirements.txt` | Python deps |
| `cmd/audio-worker/Dockerfile` | Container definition |
| `cmd/audio-worker/app/config/settings.py` | Pydantic settings |
| `cmd/audio-worker/tests/__init__.py` | Test package init |
| `cmd/audio-worker/tests/test_segment_chunker.py` | Chunker unit tests |
| `cmd/image-worker/worker.py` | Main image worker (async, aio_pika) |
| `cmd/image-worker/requirements.txt` | Python deps |
| `cmd/image-worker/Dockerfile` | Container definition |
| `cmd/image-worker/app/config/settings.py` | Pydantic settings |
| `cmd/image-worker/tests/__init__.py` | Test package init |
| `cmd/image-worker/tests/test_worker.py` | Worker unit tests |

---

## Files to Modify

| File | Change |
|------|--------|
| `internal/models/job.go` | Add `ContentType`, `AudioMetadata`, `ImageMetadata`, `AudioSegment`, `StatusTranscribing`, `StatusAnalyzingImage`; update `JobMessage` with `ContentType` and `Diarize` fields |
| `internal/config/config.go` | Add `AudioQueue`, `ImageQueue` |
| `cmd/orchestrator/main.go` | Add audio extensions to whitelist, `diarize` form field, routing by content type, `MAX_AUDIO_SIZE_MB` check |
| `deploy/docker/docker-compose.yml` | Add `audio-worker` and `image-worker` services (preserve all existing services) |
| `.env.example` | Add Whisper, multimodal LLM, queue, and size limit env vars |
| `Makefile` | Add `run-audio-worker`, `run-image-worker`; add them to `run-workers` without removing `run-extraction-worker` |
| `docs/swagger/docs.go` | Add audio file types, `diarize` param, new job statuses |

---

## Testing Strategy

### Unit tests

- Client pool failover and retry logic (mocked HTTP responses).
- Thread-safe round-robin URL selection.
- Speaker-aware chunking edge cases (single speaker, speaker changes at max_chars boundary).
- Chunk output format matches `chunk_text()` from extraction-worker.
- Go model types: JSON serialization roundtrip for `AudioMetadata`, `ImageMetadata`.

### Integration tests (requires running services)

1. Start infra: `make infra-up`
2. Start audio-worker pointing to a local mock Whisper service.
3. Submit `.mp3` file via `POST /upload`.
4. Poll job status until `completed` or `failed`.
5. Verify `orchestrator:job:{id}:text` is populated in Redis.
6. Verify `orchestrator:job:{id}:chunks` is a non-empty JSON array.
7. Verify embeddings queue received a message with `chunks` field.

### Manual testing checklist

- [ ] Upload `.mp3` → job status goes `transcribing` → `completed`
- [ ] Upload `.jpg` → job status goes `analyzing_image` → `completed`
- [ ] Whisper URL unreachable → retries 3 times → job status `failed`
- [ ] Two Whisper URLs, first fails → failover to second → `completed`
- [ ] PDF upload still works unchanged
- [ ] Diarized audio produces `[int1]:` / `[int2]:` prefixed chunks in Redis
- [ ] Audio > `MAX_AUDIO_SIZE_MB` → job status `failed` with size error
- [ ] `completion-worker` correctly completes audio/image jobs (verify `steps` key is set)

---

## Success Criteria

- ✓ Audio and image files accepted at `POST /upload` without 400 errors
- ✓ Job status reflects new `transcribing` / `analyzing_image` intermediate states
- ✓ Extracted text stored in Redis under `orchestrator:job:{id}:text`
- ✓ Chunks stored in Redis under `orchestrator:job:{id}:chunks` with correct format
- ✓ Downstream workers (embeddings, entities, metadata) receive the job with `chunks` field
- ✓ Failover: single Whisper/LLM URL failure does not fail the job if a second URL is available
- ✓ Air-gapped: no internet access required at runtime; workers load no local models
- ✓ All existing document tests still pass
- ✓ `run-workers` Makefile target still starts extraction-worker

---

## Future Enhancements

Not in scope, but noted for later:

1. **Streaming transcription** — return partial results via SSE while audio is processed
2. **Language detection** — pass detected language downstream to entities-worker for language-aware NER
3. **Video support** — extract audio track from `.mp4` before sending to Whisper
4. **Confidence threshold** — discard image analysis results below a configurable confidence floor
5. **Async polling for large files** — already implemented here via `run_in_executor`; future improvement would use native async HTTP client (aiohttp) in the client pools

---

## References

- `AGENTS.md` — build commands, code style, air-gapped requirements
- `pkg/worker_common/base.py` — BaseWorker (synchronous pika pattern; NOT used by audio/image workers)
- `cmd/extraction-worker/worker.py` — **reference pattern for audio/image workers**: async aio_pika, `run_in_executor` for blocking calls, `hset` for status/steps, downstream message format with `chunks`
- `internal/models/job.go` — existing job status and data model definitions
- `deploy/docker/docker-compose.yml` — service, network, and volume definitions
- `docs/plans/2026-03-16-gpu-images-spreadsheets-plan.md` — previous plan for image extension whitelist
