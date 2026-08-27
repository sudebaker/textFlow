# System Architecture

## System Overview

Event-driven microservices system for extracting and processing documents. Orchestrates extraction, metadata, embeddings, entities, and optional inference extraction from PDFs, DOCX, and other document formats. Air-gapped capable (all ML models run locally).

## Key Components

- **Orchestrator** (Go, port 8080): REST API, job orchestration, Redis state management
- **Resource Manager** (Go, port 9090): GPU memory monitoring
- **Extraction Worker** (Python): Docling integration, text chunking, source classification
- **Embeddings Worker** (Python): BAAI/bge-m3 embeddings
- **Entities Worker** (Python): GLiNER NER extraction
- **Metadata Worker** (Python): Lightweight text analytics
- **Inference Worker** (Python): Optional LLM-based fact extraction
- **Completion Worker** (Python): Results aggregation, webhook notifications
- **Redis**: State persistence, 24h TTL
- **RabbitMQ**: Async message queuing
- **Docling**: Document extraction service (optional, self-hosted)
- **vLLM**: LLM serving (optional, for inferences)

## Architecture Diagram

```mermaid
graph TB
    Client["Client"]
    Client -->|POST /documents| Orchestrator["Orchestrator<br/>(Go, port 8080)"]
    
    Orchestrator -->|validate| Redis[("Redis<br/>(State Store)")]
    Orchestrator -->|publish| Q_Extract["RabbitMQ<br/>extract_text"]
    
    Q_Extract --> ExtractionWorker["Extraction Worker<br/>(Python)"]
    ExtractionWorker -->|Docling REST| Docling["Docling<br/>(port 5001)"]
    ExtractionWorker -->|store text,chunks refs (FS artifact store)| Redis
    ExtractionWorker -->|publish| Q_Embed["RabbitMQ<br/>embeddings"]
    ExtractionWorker -->|publish| Q_Ent["RabbitMQ<br/>entities"]
    ExtractionWorker -->|publish| Q_Meta["RabbitMQ<br/>metadata"]
    ExtractionWorker -->|publish?| Q_Inf["RabbitMQ<br/>inferences"]
    
    Q_Embed --> EmbeddingsWorker["Embeddings Worker<br/>(BAAI/bge-m3)"]
    Q_Ent --> EntitiesWorker["Entities Worker<br/>(GLiNER)"]
    Q_Meta --> MetadataWorker["Metadata Worker<br/>(text analytics)"]
    Q_Inf --> InferenceWorker["Inference Worker<br/>(vLLM)"]
    
    EmbeddingsWorker -->|store embeddings ref (FS)| Redis
    EntitiesWorker -->|store entities| Redis
    MetadataWorker -->|store metadata| Redis
    InferenceWorker -->|vLLM REST| vLLM["vLLM<br/>(optional)"]
    InferenceWorker -->|store inferences| Redis
    
    Redis -->|pub/sub: job:events| CompletionWorker["Completion Worker<br/>(aggregator)"]
    CompletionWorker -->|finalize| Orchestrator
    CompletionWorker -->|POST| Webhook["Client Webhook"]
    
    ResourceManager["Resource Manager<br/>(GPU monitoring)"]
    
    style Orchestrator fill:#4A90E2,stroke:#2E5C8A,color:#fff
    style Redis fill:#E74C3C,stroke:#A73B2D,color:#fff
    style ExtractionWorker fill:#27AE60,stroke:#1B6D42,color:#fff
    style EmbeddingsWorker fill:#27AE60,stroke:#1B6D42,color:#fff
    style EntitiesWorker fill:#27AE60,stroke:#1B6D42,color:#fff
    style MetadataWorker fill:#27AE60,stroke:#1B6D42,color:#fff
    style InferenceWorker fill:#27AE60,stroke:#1B6D42,color:#fff
    style CompletionWorker fill:#8E44AD,stroke:#6C3483,color:#fff
```

## Data Flow

The typical document processing flow:

1. **Upload**: Client performs `POST /documents` with `document_base64` or `document_url`
2. **Creation**: Orchestrator creates job, stores in Redis, publishes to extraction queue
3. **Extraction**: Extraction worker downloads/decodes, extracts text, chunks it, classifies source, publishes to embeddings/entities/metadata/inferences queues
4. **Processing**: 4 workers (or 3 if no inferences) process in parallel, store results in Redis (text/chunks/embeddings as `sha256:` refs pointing to the FS artifact store; entities/metadata as raw values)
5. **Completion**: CompletionWorker watches Redis pub/sub, when all steps done, finalizes job, saves to file, sends webhook

## Redis Key Schema

Namespaced key structure with pattern `{namespace}:job:{jobID}:{field}`:

```
Status/Lifecycle:
  orchestrator:job:abc123:status → "completed"
  orchestrator:job:abc123:error → "error message"
  orchestrator:job:abc123:created_at → 1234567890
  orchestrator:job:abc123:completed_at → 1234567999

Data Results (artifact refs → FS artifact store):
  orchestrator:job:abc123:text → ref sha256:<hex> (payload on FS)
  orchestrator:job:abc123:chunks → ref sha256:<hex> (payload on FS)
  orchestrator:job:abc123:embeddings → ref sha256:<hex> (payload on FS)

Data Results (raw in Redis):
  orchestrator:job:abc123:entities → JSON array
  orchestrator:job:abc123:metadata → JSON object

Processing State:
  orchestrator:job:abc123:steps → {"extraction": "completed", "embeddings": "completed", ...}
  orchestrator:job:abc123:features → JSON array (requested features)

Results:
  :results no longer lives in Redis; the completion worker writes aggregated
  results to results-data/{jobID}.json

Optional:
  orchestrator:job:abc123:inferences → JSON array (if requested)
  orchestrator:job:abc123:llm_model → "gpt-3.5-turbo"
  orchestrator:job:abc123:llm_max_len → 512
```

Redis keys (refs, control) expire after **24 hours** (configurable via `JOB_TTL` env var); FS artifact store blobs do not expire.

## Environment Variables

Key configuration variables:

- `REDIS_URL`: Redis connection string (default: `redis://localhost:6379`)
- `RABBITMQ_URL`: RabbitMQ AMQP URL (default: `amqp://localhost:5672/`)
- `DOCLING_URL`: Docling service endpoint (default: `http://docling:5001`)
- `LLM_URL`: OpenAI-compatible LLM endpoint for inferences (default: empty, inferences disabled)
- `INFERENCE_ADAPTIVE_ENABLED`: Enable adaptive LLM concurrency control (default: `false`)
- `JOB_TIMEOUT`: Job processing timeout (default: `60m`)
- `JOB_TTL`: Redis key expiration (default: `24h`)
- `WEBHOOK_URL`: Optional client webhook for notifications
- `HF_HUB_OFFLINE`: Set to `1` for air-gapped mode (embeddings/entities workers)
- `TRANSFORMERS_OFFLINE`: Set to `1` for air-gapped mode

## Error Handling & Resilience

### Circuit Breaker Pattern
External service failures (vLLM, Docling) are protected by circuit breaker:
- **Closed**: Normal operation
- **Open**: Too many failures, block all requests
- **Half-Open**: Testing probes after timeout

Default thresholds: 60% failure rate, 3+ requests to trip.

### Retry Policy
Exponential backoff applied to transient failures:
- Initial delay: 1s
- Multiplier: 2x
- Max delay: 10s
- Max retries: 3

Used by: Pipeline (orchestration failures), workers (network hiccups).

### Graceful Degradation
If optional services unavailable:
- vLLM/Ollama unavailable → Inferences skipped, job completes without inferences
- Webhook URL not set → Notification optional, no error
- Docling temporarily down → Retry with circuit breaker

### Adaptive LLM Concurrency (inference-worker)
The inference-worker ships an `AdaptiveSemaphore` (AIMD congestion control, gated behind `INFERENCE_ADAPTIVE_ENABLED`, default `false`). With it disabled, behavior is identical to the previous fixed-concurrency path. When enabled:

- **Binary signal (TCP Reno):** each successful LLM call is an ACK that grows the window (`cwnd + 1`); each error/timeout halves it (`cwnd // 2`). `LLM_TIMEOUT` acts as the TCP RTO.
- **Degradation:** if `acquire` fails (cooldown/saturation), the worker returns an empty result silently rather than blocking the pipeline.
- **Prefetch:** `ADAPTIVE_MAX_CONCURRENCY + BATCH_SIZE` when batching, else `ADAPTIVE_MAX_CONCURRENCY` (unless `PREFETCH_COUNT` is set explicitly).
- **Metrics:** `inference_worker_cwnd`, `inference_worker_in_flight`, `inference_worker_cooldown` (gauges), `inference_worker_llm_requests_total`, `inference_worker_llm_timeouts_total` (counters). `inference_worker_llm_tokens_per_sec` is observability-only — not a control signal.

The worker stays pika `BlockingConnection` single-threaded; the semaphore gates the LLM call but no thread pool touches pika.

### Client Backpressure (tools/client)

`bin/client` honors admission-control responses:
- On `429` / `503`, reads the `Retry-After` header and waits, falling back to exponential backoff (`1 << attempt` seconds) up to 5 retries.
- Respects the request context so backoff cancels cleanly on timeout.

### Stuck Job Expiration
Jobs that exceed `JOB_TIMEOUT` are forcefully expired:
- Status set to `failed`
- Error message recorded
- Redis keys expire after `JOB_TTL` (24h default)

## Scalability Notes

- **RabbitMQ Prefetch**: Configurable per worker (default: 3 messages) to prevent overload
- **Redis Single-Threaded**: Monitor for bottlenecks; consider Redis Cluster for high volume
- **Worker Concurrency**: Each worker processes multiple jobs via async I/O (Python `asyncio`, Go goroutines)
- **No Leader Election**: Any number of worker replicas can run simultaneously (horizontal scaling)
- **Idempotent Operations**: Safe to restart workers or duplicate messages; deduplication via job ID
- **State Versioning**: Redis key schema allows migration without downtime

## Deployment Models

### Single-Node Development
```bash
make infra-up          # Start RabbitMQ, Redis, Docling
make run-orchestrator  # Port 8080
make run-workers       # All workers
```

### Air-Gapped Production
Mount model directories (no internet at build/runtime):
```bash
docker run -v /models/bge-m3:/models/bge-m3 \
           -v /models/gliner:/models/gliner \
           -e HF_HUB_OFFLINE=1 \
           -e TRANSFORMERS_OFFLINE=1 \
           embeddings-worker
```

### Kubernetes
Deploy orchestrator, resource-manager as services; workers as StatefulSet or Deployment with horizontal autoscaling based on RabbitMQ queue depth.

## Health & Monitoring

- **HTTP /health**: Orchestrator health endpoint
- **Prometheus /metrics**: Worker metrics (job counts, durations, queue depth)
- **Redis PING**: Health check for state store
- **RabbitMQ Management**: Queue monitoring at http://localhost:15672

Recommended alerts:
- Circuit breaker open (external service down)
- Redis CPU >80%
- RabbitMQ queue depth >1000
- Job timeout rate increasing
- Worker memory usage >90%
