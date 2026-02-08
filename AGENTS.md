# AGENTS.md - Development Guidelines for IA Text Orchestrator

This document provides guidelines for AI agents working on this codebase. The project is an event-driven microservices architecture with Go (orchestrator) and Python (workers).

## Project Structure

```
ia-text-orchestrator/
├── cmd/
│   ├── orchestrator/           # Go REST API (port 8080)
│   │   └── main.go
│   ├── resource-manager/       # Go resource manager (port 9090)
│   │   └── main.go
│   ├── embeddings-worker/      # Python: Text embeddings (BAAI/bge-m3)
│   │   ├── worker.py
│   │   └── requirements.txt
│   ├── entities-worker/        # Python: Named entity extraction (GLiNER)
│   │   ├── worker.py
│   │   └── requirements.txt
│   ├── extraction-worker/     # Python: Text extraction (Unstructured API)
│   │   └── worker.py
│   ├── metadata-worker/       # Python: Metadata analysis
│   │   └── worker.py
│   └── completion-worker/      # Python: Job completion aggregator
│       └── worker.py
├── internal/                   # Go shared packages
│   ├── broker/rabbitmq.go      # RabbitMQ client
│   ├── redis/client.go         # Redis client
│   ├── events/event_bus.go    # Redis Pub/Sub event bus
│   ├── config/config.go       # Configuration
│   ├── middleware/            # Circuit breaker, rate limit, retry
│   └── health/checker.go      # Health checks
├── pkg/                        # Shared utilities
│   ├── logging/logger.go      # Structured logging (Go)
│   ├── metrics/metrics.go      # Prometheus metrics
│   ├── worker_common/         # Python worker utilities
│   └── events_python.py       # Python event bus
└── deploy/docker/              # Docker Compose
```

## Architecture Overview

```
Document → [Orchestrator:8080] → RabbitMQ → [Extract Worker] → 3 parallel queues
                                                    ├→ [Embeddings Worker]
                                                    ├→ [Entities Worker]
                                                    └→ [Metadata Worker]
                                                              ↓
                                                    [Completion Worker] → Redis
                                                              ↓
                                                    Status: completed
```

## Build/Lint/Test Commands

### Makefile (Preferred)
```bash
make help                         # Show all available commands

# Development
make run-orchestrator             # Run orchestrator on port 8080
make run-resource                 # Run resource manager on port 9090
make run-embeddings-worker        # Run embeddings worker
make run-entities-worker          # Run entities worker
make run-workers                  # Run all Python workers
make run-all                      # Run all services locally

# Infrastructure
make infra-up                     # Start RabbitMQ, Redis, Unstructured
make infra-down                   # Stop infrastructure
make docker-up                    # Start all with docker-compose
make docker-down                  # Stop all services
make docker-logs                  # Follow all logs

# Testing
make test                         # Run all Go tests
make test-coverage               # Run tests with coverage HTML
make test-python                 # Run all Python tests
pytest cmd/*/tests -v            # All Python tests with verbose

# Quality
make lint                         # Run Go linter (golangci-lint)
make lint-fix                     # Fix linter issues
make format                       # Format Go and Python code

# Build
make build                        # Build all Go binaries
make build-orchestrator           # Build orchestrator binary
make build-resource-manager      # Build resource-manager binary
```

### Running Single Tests

**Go:**
```bash
go test -v ./internal/redis/...              # Single package
go test -v ./internal/redis/client_test.go   # Single file
go test -v -run TestSetJobStatus ./...       # Single test function
go test -v -cover ./...                      # With coverage
```

**Python:**
```bash
pytest cmd/embeddings-worker/tests/ -v                       # Single worker tests
pytest cmd/embeddings-worker/tests/test_api.py -v           # Single test file
pytest cmd/embeddings-worker/tests/test_api.py::test_extract_success -v  # Single test
pytest cmd/*/tests -v --cov=app --cov-report=html           # With coverage HTML
```

### Docker Commands
```bash
docker compose build              # Build all images
docker compose up -d              # Start all detached
docker compose logs -f orchestrator  # Follow orchestrator logs
docker compose exec -it redis redis-cli  # Access Redis CLI
```

## Code Style Guidelines

### Imports (Python)
Organize in three sections, sorted alphabetically:
```python
import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import Dict, Optional

import pika
import redis
from fastapi import FastAPI

from app.config.settings import Settings
from app.services.embeddings import EmbeddingService
```

### Types (Python)
Use Python 3.11+ type hints with Pydantic:
```python
from typing import List, Dict, Optional
from pydantic import Field, BaseModel

class JobRequest(BaseModel):
    job_id: str = Field(..., description="Unique job identifier")
    document_base64: Optional[str] = None
```

### Naming Conventions
- **Classes**: PascalCase (e.g., `EmbeddingService`, `RedisClient`)
- **Functions/Variables**: snake_case (e.g., `generate_embeddings`, `job_id`)
- **Constants**: UPPER_SNAKE_CASE (e.g., `MAX_RETRIES`)
- **Private members**: Leading underscore (e.g., `_redis_client`)
- **Go**: Use camelCase for exported, camelCase for unexported

### Error Handling (Python)
```python
try:
    result = await process_document(doc)
except ValueError as e:
    logger.warning(f"Validation error: {e}")
    raise HTTPException(status_code=400, detail=str(e))
except Exception as e:
    logger.error(f"Processing failed: {e}")
    raise HTTPException(status_code=500, detail="Processing error")
```

### Error Handling (Go)
```go
func processDocument(doc Document) error {
    result, err := redis.GetJobStatus(ctx, jobID)
    if err != nil {
        logger.Error().Err(err).Msgf("Failed to get status for job %s", jobID)
        return fmt.Errorf("job not found: %w", err)
    }
    return nil
}
```

### FastAPI Patterns (Python Workers)
```python
@router.post("/process", response_model=JobResponse)
async def process_document(request: JobRequest) -> JobResponse:
    """Process a document and return job ID."""
    # implementation
```

### Go Patterns (Gin Framework)
```go
func createJobHandler(c *gin.Context) {
    var req models.CreateJobRequest
    if err := c.ShouldBindJSON(&req); err != nil {
        c.JSON(http.StatusBadRequest, models.ErrorResponse{
            Error: "invalid_request",
            Detail: err.Error(),
        })
        return
    }
    // implementation
}
```

### Logging
Use structured logging with context:
```python
# Python
logger.info(f"Processing job: {job_id}", extra={"job_id": job_id})
```

```go
// Go
logger.Info().
    Str("job_id", jobID).
    Str("status", status).
    Msg("Job status updated")
```

### Configuration
Use environment variables with validation:
```python
# Python
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    redis_url: str = "redis://localhost:6379"
    rabbitmq_url: str
    
    class Config:
        env_prefix = "APP_"
```

```go
// Go
type Config struct {
    RabbitMQURL string `env:"RABBITMQ_URL,required"`
    RedisURL    string `env:"REDIS_URL" default:"redis://localhost:6379"`
    HTTPPort    int    `env:"HTTP_PORT" default:"8080"`
}
```

### Testing
```python
# pytest pattern
class TestWorker:
    def setup_method(self):
        self.worker = create_test_worker()
    
    def test_process_document_success(self):
        result = self.worker.process("test_doc")
        assert result.status == "completed"
```

```go
// Go pattern
func TestRedisClient_SetJobStatus(t *testing.T) {
    client := NewTestClient()
    err := client.SetJobStatus(ctx, "job123", "pending")
    assert.NoError(t, err)
}
```

## Important Patterns

### Redis Keys (Namespaced)
All Redis keys use namespace prefix (default: "orchestrator"):
- `orchestrator:job:{id}:status` - Job status
- `orchestrator:job:{id}:text` - Extracted text
- `orchestrator:job:{id}:embeddings` - Embedding vectors
- `orchestrator:job:{id}:entities` - Named entities
- `orchestrator:job:{id}:metadata` - Document metadata

### RabbitMQ Queues
- `extract_text` - Document extraction
- `embeddings` - Embedding generation
- `entities` - Named entity extraction
- `metadata` - Metadata analysis

### Event Bus (Redis Pub/Sub)
Used for cross-service communication:
- `job:created` - New job initiated
- `step:completed` - Processing step finished
- `job:completed` - All steps done

## Environment Variables

Required for all services:
```bash
REDIS_URL=redis://localhost:6379
RABBITMQ_URL=amqp://localhost:5672/
UNSTRUCTURED_URL=http://localhost:8000
```

Optional:
```bash
APP_LOG_LEVEL=info          # debug, info, warn, error
APP_HTTP_PORT=8080          # Default: 8080 (orchestrator)
```

## Quick Start for Development

```bash
# 1. Start infrastructure
make infra-up

# 2. Verify services are running
curl http://localhost:8080/health
redis-cli ping
docker exec -it rabbitmq rabbitmqctl status

# 3. Run orchestrator (new terminal)
make run-orchestrator

# 4. Run workers (separate terminals)
make run-embeddings-worker
make run-entities-worker

# 5. Test a document
curl -X POST http://localhost:8080/v1/documents/process \
  -H "Content-Type: application/json" \
  -d '{"document_url": "https://example.com/doc.pdf"}'

# 6. Check status
curl http://localhost:8080/v1/documents/{job_id}
```

## Security Considerations

- Validate all inputs (document size limits, URL whitelisting)
- Prevent SSRF attacks (block localhost, cloud metadata endpoints)
- Use environment variables for secrets (no hardcoded credentials)
- Network segmentation (databases not exposed externally)
- Circuit breaker for external service calls

## Performance Notes

- Load ML models once at startup (not per request)
- Use connection pooling for Redis and RabbitMQ
- Context timeouts on all operations (5-30s depending on operation)
- Prefetch count for consumers (5-10 messages)
- Redis pipelining for batch operations when possible