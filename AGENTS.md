# AGENTS.md - IA Text Orchestrator

Event-driven microservices: Go orchestrator + Python workers (RabbitMQ, Redis, Unstructured API).

---

## Air-Gapped Deployment (HARD REQUIREMENT)

**This system is designed for on-premise, fully air-gapped deployment** — no internet access at build or runtime.

### Model Files (CRITICAL)

1. **Location on host:** `models/` directory (e.g., `/path/to/textflow/models/`)
2. **Mounted into containers:** `-v ../../models:/models` in docker-compose
3. **Pre-downloaded on host:** All model files must already exist locally before building:
   - `models/bge-m3/` → embeddings-worker (BAAI/bge-m3 model)
   - `models/deberta-v3-small/` → GLiNER backbone tokenizer (must have: `config.json`, `pytorch_model.bin`, `spm.model`, `tokenizer_config.json`)
   - `models/gliner-small-v2.1/` → GLiNER entity extractor (must have: `gliner_config.json`, `pytorch_model.bin`)
   - `models/modern-gliner/` → embeddings-worker GLiNER variant

### Docker Build Rules

- ✅ **Allowed:** `pip install`, `go get` (build-time dependencies)
- ❌ **FORBIDDEN:** `RUN python download_*.py`, `wget model_url`, `HF hub downloads`, HuggingFace Hub API calls at build time
- ✅ **Enforced:** `ENV HF_HUB_OFFLINE=1` + `ENV TRANSFORMERS_OFFLINE=1` in production Dockerfiles (after model loading code)
- ✅ **Required:** `local_files_only=True` when loading models from transformers/GLiNER

### Model Config Paths

- `models/gliner-small-v2.1/gliner_config.json` must have `"model_name": "/models/deberta-v3-small"` (absolute path, not HF identifier)
- DeBERTa tokenizer files already exist at `models/deberta-v3-small/` (no separate download needed)

### Verification

Test offline mode:
```bash
docker run --network=none entities-worker  # Should start without internet
```

---

## Known Issues (CRITICAL)

1. **Entities Worker Offline**: GLiNER with local models now works correctly with `local_files_only=True` + offline env vars
2. **Entity Deduplication**: Set `DEDUPLICATION_ENABLED=false` (threshold too aggressive)
3. **Date/Money Thresholds**: Use `ENTITY_THRESHOLD_DATE=0.45`, `MONEY=0.55`

---

## Build / Test Commands

```bash
make help                  # Show all commands
make infra-up             # Start RabbitMQ, Redis, Unstructured
make infra-down           # Stop infrastructure
make docker-up/down       # Start/stop all with docker-compose
make run-orchestrator     # Run orchestrator (port 8080)
make run-embeddings-worker
make run-entities-worker
make run-workers          # All workers
make test                 # Go tests
make test-coverage        # With coverage HTML
make test-python          # Python tests
make lint / lint-fix      # golangci-lint
make format               # go fmt + black + isort
make build                # Build binaries
```

### Single Tests

**Go:** `go test -v ./internal/redis/...` | `-run TestFunc`

**Python:** `pytest cmd/embeddings-worker/tests/test_api.py -v` | `::test_name`

---

## Project Structure

```
cmd/              # Services: orchestrator (Go, 8080), resource-manager (Go, 9090),
                  # embeddings-worker, entities-worker (⚠️), extraction-worker, 
                  # metadata-worker, completion-worker (Python)
internal/         # Go: broker/, config/, events/, middleware/, models/, redis/
pkg/              # Shared: logging/, metrics/, events_python.py, worker_common/base.py
```

---

## Python Worker Pattern (BaseWorker)

```python
import sys; sys.path.insert(0, "/app")
from pkg.worker_common.base import BaseWorker

class MyWorker(BaseWorker):
    def __init__(self):
        super().__init__(worker_name="my-worker", queue_name="my_queue", 
                         metrics_port=8001, requires_gpu=False)

    def process_message(self, message: dict):
        return result  # Store via self.redis_client, publish via self.event_bus

if __name__ == "__main__":
    MyWorker().run()
```

---

## Code Style

### Imports (3 sections, alphabetical)
```python
# Standard library
import logging, os
from typing import Dict, List, Optional

# Third-party
import pika, redis
from fastapi import FastAPI
from pydantic import BaseModel, Field

# Local
from pkg.worker_common.base import BaseWorker
```

### Naming
| Element | Convention | Example |
|---------|------------|---------|
| Python classes | PascalCase | `EmbeddingService` |
| functions/vars | snake_case | `generate_embeddings` |
| constants | UPPER_SNAKE | `MAX_RETRIES` |
| private | underscore | `_redis_client` |
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

### Config
```python
# Python: pydantic_settings.BaseSettings with env_prefix
# Go: struct with env tags: `env:"VAR_NAME,required"` or `default:"value"`
```

---

## Architecture

| Service | Lang | Port | Notes |
|---------|------|------|-------|
| orchestrator | Go/Gin | 8080 | REST API, SSRF validation |
| resource-manager | Go | 9090 | GPU monitoring |
| embeddings-worker | Python | - | BAAI/bge-m3 |
| entities-worker | Python | - | GLiNER ⚠️ offline |
| extraction-worker | Python | - | Unstructured API |

**Redis keys:** `orchestrator:job:{id}:{status|text|chunks|embeddings|entities|metadata|results}`

---

## Required Env Vars

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

---

## Quick Start

```bash
make infra-up
curl http://localhost:8080/health
make run-orchestrator
make run-embeddings-worker && make run-entities-worker
curl -X POST http://localhost:8080/v1/documents/process \
  -H "Content-Type: application/json" \
  -d '{"document_url": "https://example.com/doc.pdf"}'
redis-cli GET "orchestrator:job:{job_id}:entities"
```
