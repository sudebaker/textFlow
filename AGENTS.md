# AGENTS.md - IA Text Orchestrator

Event-driven microservices: Go orchestrator + Python workers (RabbitMQ, Redis, Unstructured API).

---

## Build / Test Commands

```bash
make help                  # Show all commands
make infra-up              # Start RabbitMQ, Redis, Docling
make infra-down            # Stop infrastructure
make docker-up/down        # Start/stop all with docker-compose
make run-orchestrator      # Run orchestrator (port 8080)
make run-embeddings-worker # Run embeddings worker
make run-entities-worker   # Run entities worker ⚠️ offline-critical
make run-workers           # All workers
make test                  # Run all Go tests
make test-coverage         # With coverage HTML
make test-python           # Run all Python tests
make lint / lint-fix       # golangci-lint
make format                # go fmt + black + isort
make build                 # Build binaries
```

### Single Tests

**Go:** `go test -v ./internal/redis/...` | `-run TestFunc` | `./internal/redis/client_test.go`

**Python:** 
- `pytest cmd/embeddings-worker/tests/test_chunking.py -v`
- `pytest cmd/embeddings-worker/tests/test_chunking.py::TestChunkingService::test_basic_chunking -v`
- `pytest cmd/entities-worker/tests/test_api.py -v`

**Entities Worker Offline:**
```bash
python cmd/entities-worker/offline_diagnosis.py
python cmd/entities-worker/test_offline_ner.py
docker run --network=none entities-worker python test_offline_ner.py
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

**Python:**
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

**Go:**
```go
result, err := client.GetJobStatus(ctx, jobID)
if err != nil {
    logger.Error().Err(err).Str("job_id", jobID).Msg("Failed to get status")
    return fmt.Errorf("job not found: %w", err)
}
```

### Config

**Python:** `pydantic_settings.BaseSettings` with `env_prefix`
**Go:** struct with `env:"VAR_NAME,required"` or `default:"value"` tags

---

## Project Structure

```
cmd/              # Orchestrator (Go, 8080), resource-manager (Go, 9090),
                  # embeddings-worker, entities-worker, extraction-worker,
                  # metadata-worker, completion-worker (Python)
internal/         # Go: broker/, config/, events/, middleware/, models/, redis/
pkg/              # shared: logging/, metrics/, events_python.py, worker_common/base.py
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

## Air-Gapped Deployment (CRITICAL)

**No internet at build or runtime.**

### Model Files

Mount `-v ../../models:/models` with:
- `models/bge-m3/` → embeddings-worker
- `models/deberta-v3-small/` → GLiNER backbone (config.json, pytorch_model.bin, spm.model, tokenizer_config.json)
- `models/gliner-small-v2.1/` → GLiNER extractor (gliner_config.json, pytorch_model.bin)
- `models/modern-gliner/` → embeddings-worker GLiNER variant

### Docker Build Rules

- ✅ `pip install`, `go get`
- ❌ `RUN python download_*.py`, `wget`, HF Hub API
- ✅ `ENV HF_HUB_OFFLINE=1` + `ENV TRANSFORMERS_OFFLINE=1`
- ✅ `local_files_only=True` when loading models

### Verification

```bash
docker run --network=none entities-worker
