# textFlow — Architecture

> See `docs/MODELS.md` for model inventory, `docs/GPU.md` for CUDA/Docling
> specifics, `configs/pipeline.json` as the declarative DAG.

## Pipeline

```
API (POST /v1/documents)
  ↓  validate + store pending + stage.queued
RabbitMQ (extract_text | audio | image — entry)
  ↓  PipelineDefinition.queues_for (configs/pipeline.json)
extraction-worker (or audio-worker / image-worker for multimodal)
  ↓  Docling REST (HTTP async polling) or Whisper / image-analyzer
  → Redis: orchestrator:job:{id}:text|chunks (sha256:<hex> refs, FS artifact store)
  → Redis: metadata:document, metadata:text, source_classification
  → stage.completed(extraction)
  ↓  fan-out per PipelineDefinition.steps_for
RabbitMQ (embeddings | entities | metadata | [inferences])
  ↓
embeddings-worker (BAAI/bge-m3)  → Redis :embeddings (msgpack blob via FS, noeviction)
entities-worker (GLiNER + deberta backbone + regex parallel) → Redis :entities_raw + fan-out inferences if enabled
metadata-worker (text analytics) → Redis :metadata:text
inference-worker (vLLM/Ollama OpenAI-compat) → Redis :micro_inferences (per-chunk grouped JSON)
  ↓  EventBus pub/sub
completion-worker (pub/sub stage.completed)
  ↓  when required_steps ⊆ completed_steps
finalize → results-data/{jobID}.json + Redis results ref + stage.completed + job_progress 100
  ↓
GET /v1/documents/:id/download  (chunk-level inferences, embeddings via gzip, entities dict)
```

## Component responsibilities

### Orchestrator (Go, 8080)
`cmd/orchestrator` (Gin). Admission via `internal/broker/rabbitmq.go` (backpressure on `QueueDepthRejectThreshold`), SSRF guard on `DocumentURL`, `POST /v1/documents`, `GET /v1/documents/:id`, `POST /v1/documents/:id/cancel` (`status=cancelled`), batch `CreateBatchHandler`. Ver §23 rabbitmq `3.13-management` drift fixed.

### Redis (state)
`internal/redis/client.go`. Content keys + refs (`sha256:<hex>` → FS blob), control keys (`:status:steps`, `:features:profile`, `:inferences:remaining|total`). TTL 24h on job keys; pub/sub `job:events`, `job:{id}:events` (`stage.queued/started/completed/failed`, `job:created/progress/completed/failed`). Maxmemory `noeviction` — large blobs live in FS.

### RabbitMQ
`internal/broker/rabbitmq.go`. DLX `document_processor_dlx` + DLQ, delayed exchange `document_processor_delayed` for retries, pool `ChannelPool`. Metrics `QueueDepth`, `QueueConsumerLag`. Preflight derives built images from compose.

### EventBus
`internal/events/event_bus.go` (Go) + `pkg/events_python.py` (Python). Spec 4.5 `stage.queued → RabbitMQ → stage.started → stage.completed/failed`. Fase 3 added `stage.queued` best-effort before each `publish`.

### Artifact Store
`pkg/worker_common/artifact_store.py` (`FSStore`, 65k sharded, atomic write, `65k buckets`). Keys migrated: `:text`, `:chunks`, `:embeddings`, `:inference_embeddings`, `:results` refs; `:micro_inferences_raw` still in Redis. GC `pkg/worker_common/artifact_gc.py` (reachability, min-age 24h).

### Profiles & PipelineDefinition
`configs/pipeline.json` (`PipelineDefinition` in `pkg/worker_common/pipeline_config.py`). `profiles: fast/balanced/full` (full intentionally == balanced, §8 / Fase 4; `inferences` only via `feature_extras` with `-f`), `pipelines.spreadsheet` (entities-only), `audio_replaces_extraction`/`image_replaces_extraction`, `feature_extras.inferences → step/queue`.

### Stages → workers

| Stage | Queue | Worker | Notes |
|-------|-------|--------|-------|
| extraction | extract_text | extraction-worker (async, PREFETCH=EXTRACTION_CONCURRENCY, Docling polling) | routing via `queues_for`, cancellation checks before metadata/publish |
| audio | audio | audio-worker (whisper, `whisper:9666/transcribe`) | VAD, not yet diarized |
| image | image | image-worker (image-analyzer `POST /analyze` OCR-only, resize + `image:{hash}` cache §5.3) | LLM `LLM_BASE_URL` (Ollama dev / vLLM prod) |
| embeddings | embeddings | embeddings-worker | bge-m3, noeviction FS ref |
| entities | entities | entities-worker | GLiNER offline, regex parallel (I/O thread) fan-out to inferences |
| metadata | metadata | metadata-worker | text analytics |
| inferences | inferences | inference-worker | 4 replicas × adaptive semaphore, batch+cache, `inferences:total` + deferred assembly |

### Completion
`cmd/completion-worker/completion_worker.py`. `check_job_completion` → `PipelineDefinition.steps_for` → `finalize_job` (deduplicate entities, enrich chunks with embeddings/entity_ids/inferences+embeddings, write `results-data/{jobID}.json`). `check_and_notify_batch`.

### Stage interface & multimodal
`pkg/worker_common/stage.py` (`Stage.execute`). Multimodal path `image→OCR→text/chunks` / `audio→transcription→text/chunks`.

### Retry / Cancellation / Backpressure
Retry via DLX delayed exchange; `internal/middleware/retry.go`. Cancellation cooperative (`pkg/shared/exceptions.JobCancelledError`, `_is_cancelled` → `ack` `status=cancelled`) at stage start, before caro, entre batches, antes de publish — no mata threads/CUDA (Fase 2). Backpressure: `AdmissionController` checks rate limit + concurrent jobs + queue depth.

### Metrics
`pkg/metrics/metrics.go` (`ia_text_*`) + Python `*_job_duration_seconds`, `*_queue_time_seconds`, `inference_worker_in_flight/cwnd/cooldown`. Grafana dashboard `deploy/docker/grafana/dashboards/textflow.json` (uid `textflow-overview`, P50/P95, queue time, throughput, errors, queue depth — Fase 3).
