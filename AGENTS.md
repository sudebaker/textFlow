# AGENTS.md - IA Text Orchestrator

Event-driven microservices: Go orchestrator + Python workers (RabbitMQ, Redis, Unstructured API).

---

## Known Issues (READ BEFORE TOUCHING ANYTHING)

1. **Entities Worker Offline Mode** (CRITICAL): GLiNER makes unauthorized HuggingFace calls. Extracts ~35 entities instead of 150-200. Test with `docker run --network=none entities-worker`.
2. **Entity Deduplication**: `FUZZY_MATCH_THRESHOLD=0.85` too aggressive. Set `DEDUPLICATION_ENABLED=false`.
3. **Date/Money Thresholds**: Use `ENTITY_THRESHOLD_DATE=0.45`, `MONEY=0.55`.
4. **DEBUG Statements**: `cmd/orchestrator/main.go` lines 355-356 - remove before production.

---

## Project Structure

```
cmd/
├── orchestrator/main.go         # Go REST API (port 8080)
├── resource-manager/main.go    # Go resource monitor (port 9090)
├── embeddings-worker/worker.py # Python: BAAI/bge-m3
├── entities-worker/worker.py   # Python: GLiNER (offline issue)
├── extraction-worker/worker.py # Python: Unstructured API
├── metadata-worker/worker.py   # Python: metadata
└── completion-worker/worker.py # Python: Redis Pub/Sub

internal/
├── broker/rabbitmq.go          # RabbitMQ + DLX
├── config/config.go            # Env vars
├── events/                     # Redis Pub/Sub
├── middleware/                 # Circuit breaker, rate limiter
├── models/job.go               # Go types
└── redis/client.go             # Redis client

pkg/
├── logging/logger.go           # Go zerolog
├── metrics/metrics.go          # Prometheus
├── events_python.py            # Python EventBus
├── logging_python.py           # Python logging
└── worker_common/base.py      # BaseWorker - all workers MUST extend
```

---

## Build / Test Commands

```bash
make help                         # Show all commands
make infra-up                    # Start RabbitMQ, Redis, Unstructured
make run-orchestrator            # Run orchestrator (port 8080)
make run-embeddings-worker       # Run embeddings worker
make run-entities-worker         # Run entities worker
make test                        # Run all Go tests
make test-python                 # Run all Python tests
make lint                        # Go linter (golangci-lint)
make lint-fix                    # Fix linter issues
make format                      # go fmt + black + isort

# Single test - Go
go test -v ./internal/redis/...              # package
go test -v ./internal/redis/client_test.go   # file
go test -v -run TestSetJobStatus ./...       # function

# Single test - Python
pytest cmd/embeddings-worker/tests/test_api.py -v
pytest cmd/embeddings-worker/tests/test_api.py::test_extract_success -v
```

---

## Architecture & Redis Keys

| Service | Lang | Port | Notes |
|---------|------|------|-------|
| orchestrator | Go/Gin | 8080 | REST API, SSRF validation |
| resource-manager | Go | 9090 | GPU monitoring |
| embeddings-worker | Python | - | BAAI/bge-m3 |
| entities-worker | Python | - | GLiNER (offline issue) |
| extraction-worker | Python | - | Unstructured API |
| metadata-worker | Python | - | Document metadata |
| completion-worker | Python | - | Redis Pub/Sub |

**Redis keys:** `orchestrator:job:{id}:{status|text|chunks|embeddings|entities|metadata|results|steps|error}`

---

## Python Worker Pattern - BaseWorker

All workers MUST extend `BaseWorker`:

```python
import sys
sys.path.insert(0, "/app")
from pkg.worker_common.base import BaseWorker

class MyWorker(BaseWorker):
    def __init__(self):
        super().__init__(
            worker_name="my-worker",
            queue_name="my_queue",
            metrics_port=8001,
            requires_gpu=False,
        )

    def process_message(self, message: dict):
        job_id = message["job_id"]
        # Store results via self.redis_client
        # Publish events via self.event_bus
        return result

if __name__ == "__main__":
    MyWorker().run()  # Blocks, handles SIGTERM/SIGINT
```

---

## Code Style

### Python Imports (3 sections, alphabetical)
```python
# Standard library
import logging
import os
from typing import Dict, List, Optional

# Third-party
import pika
import redis
from fastapi import FastAPI
from pydantic import BaseModel, Field

# Local
from pkg.worker_common.base import BaseWorker
```

### Naming Conventions
| Element | Convention | Example |
|---------|------------|---------|
| Python classes | PascalCase | `EmbeddingService` |
| Python functions/variables | snake_case | `generate_embeddings` |
| Python constants | UPPER_SNAKE_CASE | `MAX_RETRIES` |
| Python private | leading underscore | `_redis_client` |
| Go exported | PascalCase | `RedisClient` |
| Go unexported | camelCase | `jobTTL` |

### Error Handling
```python
try:
    result = process(data)
except ValueError as e:
    logger.warning(f"Validation error: {e}")
    raise HTTPException(status_code=400, detail=str(e))
except Exception as e:
    logger.error(f"Processing failed: {e}")
    raise HTTPException(status_code=500, detail="Processing error")
```

```go
result, err := client.GetJobStatus(ctx, jobID)
if err != nil {
    logger.Error().Err(err).Str("job_id", jobID).Msg("Failed to get status")
    return fmt.Errorf("job not found: %w", err)
}
```

### Configuration
```python
# Python (Pydantic)
from pydantic_settings import BaseSettings
class Settings(BaseSettings):
    redis_url: str = "redis://localhost:6379"
    rabbitmq_url: str  # required - no default
    class Config:
        env_prefix = "APP_"

# Go
type Config struct {
    RabbitMQURL string `env:"RABBITMQ_URL,required"`
    RedisURL    string `env:"REDIS_URL" default:"redis://localhost:6379"`
}
```

---

## Required Environment Variables

```bash
REDIS_URL=redis://localhost:6379
RABBITMQ_URL=amqp://localhost:5672/
UNSTRUCTURED_URL=http://localhost:8000
GLINER_MODEL_PATH=/models/gliner_multitask-v0.5
TRANSFORMERS_OFFLINE=1
HF_HUB_OFFLINE=1
ALLOW_REMOTE_DOWNLOAD=false
DEDUPLICATION_ENABLED=false
ENTITY_THRESHOLD_DATE=0.45
ENTITY_THRESHOLD_MONEY=0.55
```

See `ANALISIS_ENTIDADES_WORKER.md`, `.github/copilot-instructions.md`.
