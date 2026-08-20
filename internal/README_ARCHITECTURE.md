# Internal Infrastructure Architecture

## Module Purpose

Core Go infrastructure: Broker abstraction, caching, config, health checks, middleware, models, pipeline orchestration, Redis client, event bus. 

No business logic is present in this module—all components are pure infrastructure supporting the orchestrator and external services.

## Key Components

- **broker/**: RabbitMQ abstraction (Publish, Consume, HealthCheck) — standardizes async messaging
- **cache/**: Redis-backed content cache with SHA-256 hashing for deduplication
- **config/**: Configuration loading and validation from environment variables
- **events/**: Event bus for pub/sub (job:events channel) — broadcasts job progress
- **health/**: Health check endpoints for Redis, RabbitMQ, circuit breaker state
- **middleware/**: Circuit breaker pattern, retry policy with exponential backoff
- **models/**: Data structures (Job, Entity, Chunk, Embedding, etc.)
- **pipeline/**: Orchestration logic (fan-out to workers, poll for completion)
- **redis/**: Redis client with 30+ methods for state management
- **metrics/**: Prometheus metric registration and collection

## Dependency Graph

```mermaid
graph TB
    Config["config<br/>(env parsing)"]
    Logging["pkg/logging<br/>(zerolog)"]
    
    Config --> Redis["redis<br/>(30+ state methods)"]
    Config --> Broker["broker<br/>(RabbitMQ abstraction)"]
    Logging --> Redis
    Logging --> Broker
    
    Models["models<br/>(Job, Entity, Chunk, etc.)"]
    EventBus["events<br/>(pub/sub)"]
    
    Redis --> Pipeline
    Broker --> Pipeline
    EventBus --> Pipeline
    
    Pipeline["pipeline<br/>(orchestrator)"]
    
    Middleware["middleware<br/>(circuit, retry)"]
    Cache["cache<br/>(content cache)"]
    Health["health<br/>(health checks)"]
    Metrics["metrics<br/>(Prometheus)"]
    
    Redis --> Health
    Broker --> Health
    Middleware -.->|used by| Pipeline
    Middleware -.->|used by| Broker
    
    style Config fill:#F39C12,stroke:#C87F0A,color:#fff
    style Redis fill:#E74C3C,stroke:#A73B2D,color:#fff
    style Broker fill:#E74C3C,stroke:#A73B2D,color:#fff
    style Pipeline fill:#4A90E2,stroke:#2E5C8A,color:#fff
    style Models fill:#95A5A6,stroke:#7F8C8D,color:#fff
    style EventBus fill:#3498DB,stroke:#2980B9,color:#fff
    style Middleware fill:#F39C12,stroke:#C87F0A,color:#fff
    style Health fill:#27AE60,stroke:#1B6D42,color:#fff
    style Metrics fill:#27AE60,stroke:#1B6D42,color:#fff
```

## Redis Client Architecture

The `redis/` package provides abstraction over Redis operations:

### Core Methods (Categories)

**Job State Management** (8 methods):
- `SaveJob()`: Store job metadata
- `GetJob()`: Retrieve job status and progress
- `UpdateJobStatus()`: Atomic status transition
- `GetJobError()`: Get failure reason
- `DeleteJob()`: Cleanup expired jobs

**Text & Chunks** (4 methods):
- `SaveText()`: Store extracted text
- `GetText()`: Retrieve
- `SaveChunks()`: Store chunked text array
- `GetChunks()`: Retrieve

**Embeddings** (3 methods):
- `SaveEmbeddings()`: Store vector
- `GetEmbeddings()`: Retrieve
- `SearchByEmbedding()`: Vector similarity (optional)

**Entities & Metadata** (4 methods):
- `SaveEntities()`: Store NER results
- `GetEntities()`: Retrieve
- `SaveMetadata()`: Store analytics
- `GetMetadata()`: Retrieve

**Processing Steps** (3 methods):
- `UpdateStep()`: Mark step complete
- `GetSteps()`: Retrieve all step statuses
- `IsStepCompleted()`: Query single step

**Event Publishing** (2 methods):
- `PublishJobEvent()`: Broadcast to `job:events`
- `PublishStepEvent()`: Broadcast per-job updates

**Utilities** (5 methods):
- `HealthCheck()`: PING test
- `SetTTL()`: Refresh expiration
- `KeyExists()`: Check presence
- `GetAll()`: Retrieve all fields for job
- `FlushPattern()`: Cleanup by pattern (dev only)

### Redis Key Schema (Detailed)

```
Pattern: {namespace}:job:{jobID}:{field}
Default namespace: "orchestrator"

Status/Lifecycle:
  :status → JobStatus enum (pending, processing, completed, failed)
  :error → error message (if failed)
  :created_at → Unix timestamp
  :updated_at → Unix timestamp
  :completed_at → Unix timestamp (optional)

Data Results:
  :text → ref sha256:<hex>, resolved via artifact store (payload on FS)
  :chunks → ref sha256:<hex>, resolved via artifact store
    [{"text": "...", "index": 0, "source": "page_1"}, ...]
  :embeddings → ref sha256:<hex>, resolved via artifact store (msgpack float32 vectors)
  :entities → JSON array of entity objects
    [{"entity": "John", "label": "PERSON", "score": 0.95}, ...]
  :metadata → JSON object (keywords, language, readability_score)

Processing State:
  :steps → JSON object mapping step name to status
    {"extraction": "completed", "embeddings": "completed", "entities": "pending"}
  :features → JSON array (subset of ["embeddings", "entities", "metadata", "inferences"])
  :document_type → string (pdf, docx, txt, etc.)
  :source_classification → string (journal, news, book, etc.)

Results Aggregation:
  :results does not exist as a Redis key; the completion worker writes aggregated
  results to results-data/{jobID}.json

Optional (Inferences):
  :inferences → JSON array of inference objects
    [{"fact": "...", "confidence": 0.87, "source": "chunk_5"}, ...]
  :llm_model → LLM model ID (e.g., "gpt-3.5-turbo")
  :llm_max_tokens → Max output tokens

Caching:
  :cache_key → SHA-256 hash of input for deduplication
  :cache_hit → boolean (true if result from cache)
```

**TTL (Time-To-Live)**: Redis keys (refs, control) expire after 24 hours (default `JOB_TTL=24h`); FS artifact store blobs do not expire.

## Circuit Breaker Middleware

Protects against cascading failures from external services (vLLM, Docling):

```mermaid
graph TB
    Request["Request"]
    CB_State{Circuit<br/>State?}
    
    Closed["Closed<br/>(normal)"]
    Open["Open<br/>(blocked)"]
    HalfOpen["Half-Open<br/>(probing)"]
    
    Request --> CB_State
    CB_State -->|pass through| Closed
    CB_State -->|block all| Open
    CB_State -->|1 probe allowed| HalfOpen
    
    Closed -->|failure rate >60%| Open
    Closed -->|timeout 30s| Open
    Open -->|after 30s timeout| HalfOpen
    HalfOpen -->|probe succeeds| Closed
    HalfOpen -->|probe fails| Open
    
    FailCount["Failure Counter<br/>(sliding 60s window)"]
    Closed --> FailCount
    HalfOpen --> FailCount
    FailCount -->|>60% failures| Open
    
    style Request fill:#4A90E2,stroke:#2E5C8A,color:#fff
    style Closed fill:#27AE60,stroke:#1B6D42,color:#fff
    style Open fill:#E74C3C,stroke:#A73B2D,color:#fff
    style HalfOpen fill:#F39C12,stroke:#C87F0A,color:#fff
```

**Configuration** (from `middleware/circuit_breaker.go`):
- Failure threshold: 60% (configurable)
- Min requests to evaluate: 3
- Timeout (open to half-open): 30s
- Sliding window: 60s

## Retry Policy

Exponential backoff applied to transient errors:

```
Attempt 1: Wait 1s, retry
Attempt 2: Wait 2s, retry
Attempt 3: Wait 4s, retry
Max wait: 10s (capped)
Max attempts: 3
Total max time: ~7 seconds
```

Used by:
- Pipeline: On broker publish failures
- Workers: On network timeouts
- Health checks: On connectivity issues

Jitter (±10%) prevents thundering herd.

## Event Bus Architecture

Simple pub/sub via Redis channels:

**Channels**:
- `job:events`: Broadcast to all listeners (job progress updates)
- `job:{jobID}:events`: Private per-job updates (high-volume)

**Event Types**:
```go
type Event struct {
    JobID     string                 `json:"job_id"`
    Type      string                 `json:"type"` // created, progress, completed, failed
    Timestamp time.Time              `json:"timestamp"`
    Message   string                 `json:"message"`
    Step      string                 `json:"step,omitempty"` // extraction, embeddings, etc.
    Progress  int                    `json:"progress,omitempty"` // 0-100
    Payload   map[string]interface{} `json:"payload,omitempty"`
}
```

**Consumers** (subscribe in code):
- Completion Worker: Listens to all job events
- Resource Manager: Listens for GPU-intensive jobs
- Optional webhooks: Forward events to external systems

## Health Check Subsystem

Multi-component health checks expose via HTTP `/health`:

```mermaid
graph LR
    HTTPServer["HTTP /health<br/>Endpoint"]
    
    RedisCheck["Redis Check<br/>(PING)"]
    RabbitCheck["RabbitMQ Check<br/>(AMQP connect)"]
    CircuitCheck["Circuit State<br/>(breaker status)"]
    DiskCheck["Disk Space<br/>(temp cache)"]
    
    HTTPServer --> RedisCheck
    HTTPServer --> RabbitCheck
    HTTPServer --> CircuitCheck
    HTTPServer --> DiskCheck
    
    RedisCheck -->|UP| Green["HTTP 200"]
    RabbitCheck -->|UP| Green
    CircuitCheck -->|CLOSED| Green
    DiskCheck -->|>10% free| Green
    
    RedisCheck -->|DOWN| Red["HTTP 503"]
    RabbitCheck -->|DOWN| Red
    CircuitCheck -->|OPEN| Red
    DiskCheck -->|<10% free| Red
    
    style HTTPServer fill:#4A90E2,stroke:#2E5C8A,color:#fff
    style Green fill:#27AE60,stroke:#1B6D42,color:#fff
    style Red fill:#E74C3C,stroke:#A73B2D,color:#fff
```

**Response** (JSON):
```json
{
  "status": "healthy",
  "components": {
    "redis": {"status": "up", "latency_ms": 2},
    "rabbitmq": {"status": "up", "queues": 5},
    "circuit_breaker": {"state": "closed", "failures": 0},
    "disk": {"status": "ok", "free_gb": 50}
  }
}
```

## Models (Data Structures)

Core models defined in `models/models.go`:

```go
type Job struct {
    ID          string                 `json:"id"`
    Status      JobStatus              `json:"status"` // pending, processing, completed, failed
    CreatedAt   time.Time              `json:"created_at"`
    UpdatedAt   time.Time              `json:"updated_at"`
    Document    DocumentInput          `json:"document"`
    Features    []string               `json:"features"` // embeddings, entities, metadata, inferences
    Results     JobResults             `json:"results,omitempty"`
    Error       string                 `json:"error,omitempty"`
    Steps       map[string]StepStatus  `json:"steps"`
}

type Entity struct {
    Entity string  `json:"entity"`
    Label  string  `json:"label"` // PERSON, ORG, LOCATION, etc.
    Score  float32 `json:"score"` // confidence 0-1
    Start  int     `json:"start"` // character offset
    End    int     `json:"end"`
}

type Chunk struct {
    Text           string `json:"text"`
    Index          int    `json:"index"`
    Source         string `json:"source"` // page_1, paragraph_2, etc.
    CharOffset     int    `json:"char_offset"`
    SourceClass    string `json:"source_class"`
}

type Embedding struct {
    Vector  []float32 `json:"vector"`     // 1024-dim float32
    ModelID string    `json:"model_id"`   // bge-m3
    Dim     int       `json:"dim"`        // 1024
}

type JobResults struct {
    Text       string          `json:"text"`
    Chunks     []Chunk         `json:"chunks"`
    Embeddings Embedding       `json:"embeddings,omitempty"`
    Entities   []Entity        `json:"entities,omitempty"`
    Metadata   Metadata        `json:"metadata,omitempty"`
    Inferences []Inference     `json:"inferences,omitempty"`
}
```

## Broker (RabbitMQ) Abstraction

```go
type Broker interface {
    Publish(ctx context.Context, queue string, message []byte) error
    Consume(ctx context.Context, queue string) (<-chan []byte, error)
    HealthCheck(ctx context.Context) error
    Close() error
}
```

**Implementation** (`broker/rabbitmq.go`):
- Connection pooling (default 10 connections)
- Automatic reconnection with backoff
- Prefetch tuning (default 3 messages per worker)
- Mandatory acknowledgment (ACK/NACK per message)
- Dead-letter exchange for failed messages

**Queues**:
- `extract_text`: Extraction input (fan-out from orchestrator)
- `embeddings`: Embeddings worker input
- `entities`: Entities worker input
- `metadata`: Metadata worker input
- `inferences`: Inference worker input (optional)
- `{queue}.dlx`: Dead-letter for each queue

## Cache (Content Deduplication)

Redis-backed cache prevents reprocessing identical documents:

```
SHA-256(document_content) → Redis key → cached_results_json
TTL: 7 days (configurable)
```

**Methods** (`cache/redis_cache.go`):
- `Get(key string)`: Retrieve cached results (O(1))
- `Set(key, value)`: Store results
- `Exists(key)`: Check presence without fetching
- `Delete(key)`: Evict entry
- `Clear()`: Full flush

Used by: Orchestrator before publishing to extraction queue.

## Metrics Integration

Prometheus metrics exported at `/metrics` endpoint:

**HTTP Metrics**:
- `http_requests_total`: Total requests (method, status labels)
- `http_request_duration_seconds`: Latency histogram

**Job Metrics**:
- `orchestrator_jobs_created_total`: Cumulative job submissions
- `orchestrator_jobs_completed_total`: Completed count (status label)
- `orchestrator_job_duration_seconds`: Processing time histogram

**Broker Metrics**:
- `rabbitmq_published_total`: Published messages
- `rabbitmq_consumed_total`: Consumed messages
- `rabbitmq_queue_depth`: Current queue depth gauge

**Cache Metrics**:
- `cache_hits_total`: Cache hit counter
- `cache_misses_total`: Cache miss counter
- `cache_evictions_total`: Evicted entries

All metrics are Go counters/histograms/gauges from `prometheus/client_golang`.

## Configuration Management

Single source of truth: environment variables + `.env` file

**Example `.env`**:
```
# Redis
REDIS_URL=redis://localhost:6379/0
REDIS_PASSWORD=

# RabbitMQ
RABBITMQ_URL=amqp://guest:guest@localhost:5672/

# Timeouts
JOB_TIMEOUT=60m
JOB_TTL=24h
REQUEST_TIMEOUT=30s

# Docling
DOCLING_URL=http://docling:5001

# LLM (optional)
LLM_URL=
LLM_MODEL=gpt-3.5-turbo
LLM_MAX_TOKENS=1024

# Circuit Breaker
CIRCUIT_BREAKER_THRESHOLD=0.6
CIRCUIT_BREAKER_TIMEOUT=30s

# Logging
LOG_LEVEL=info

# Webhooks
WEBHOOK_URL=
WEBHOOK_TIMEOUT=10s
```

Loaded via `config.Load()` at startup, validated for required fields.

## Testing Strategy

Unit tests per package:
- `internal/redis/*_test.go`: Redis operations (mocked Redis)
- `internal/broker/*_test.go`: Message publishing (mocked RabbitMQ)
- `internal/pipeline/*_test.go`: Orchestration logic
- `internal/middleware/*_test.go`: Circuit breaker state transitions

Integration tests:
- `internal/integration/...`: Full stack (real Redis + RabbitMQ)

Run via: `make test`, `make test-coverage`
