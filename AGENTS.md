# AGENTS.md - textFlow

**Idioma**: Responde siempre en español.

Event-driven microservices: Go orchestrator + Python workers (RabbitMQ, Redis, Unstructured API).

---

## Communication Style

**Skill:** `token-efficient-communication`

- **Compression Level:** Moderate (eliminate fluff, preserve critical context)
- **Exception Zones:** Architecture documentation (README.md, AGENTS.md), complex debugging, developer onboarding
- **Focus:** Actionable, structured responses with minimal preamble

See `~/.config/opencode/skills/token-efficient-communication/` for full details and examples.

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
make build                 # Build all binaries → bin/
make build-orchestrator    # Build orchestrator only → bin/orchestrator
make build-resource-manager # Build resource-manager → bin/resource-manager
make build-client          # Build tools/client → bin/client
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

### DAG del pipeline (IMPORTANTE)

El DAG **no** vive en el orchestrator Go. Vive en `configs/pipeline.json`
(`PipelineDefinition`): routing fan-out en `cmd/extraction-worker/worker.py`
(vía `PipelineDefinition.queues_for`) y `required_steps` en
`cmd/completion-worker/completion_worker.py` (vía `PipelineDefinition.steps_for`).
`internal/pipeline/` fue eliminado (dead code, 0 callers).

### Migración big-bang del DAG (D4)

El DAG declarativo vive en `configs/pipeline.json` (`PipelineDefinition`,
cargado por `pkg/worker_common/pipeline_config.py`). `pipeline_version` en
`JobMessage` (escape hatch): los workers lo leen pero lo ignoran si vale "v1".

Runbook de migración (big-bang con drain, NO dual-run):
1. Stop admission: no aceptar nuevos `POST /v1/documents` (pausar llamadas / LB).
2. Drain: esperar `ZCard active_jobs == 0` (jobs en vuelo completan; `JobTimeout=60m`
   acota el peor caso). Caveat: `ExpireStuckJobs` solo expira job-level
   `pending`/`processing`/`extracting`.
3. Deploy: subir imágenes nuevas (orchestrator con `pipeline_version`, workers con
   `configs/pipeline.json`).
4. Resume admission y verificar `GET /v1/documents/:id` con un job de prueba
   (spreadsheet + full + features=["inferences"]).

### Entities-worker: regex en thread paralelo (D2)

`entities_worker.py:extract_regex_parallel()` ejecuta la extracción regex
(microservicio Go vía HTTP, I/O-bound) en un `ThreadPoolExecutor(max_workers=1)`
concurrente a GLiNER (CPU/GPU-bound). El `:text` se fetcha antes del dispatch.
Degrade silencioso: si el servicio regex falla, se retorna solo GLiNER. Control:
`REGEX_ENABLED` (default true), `REGEX_SERVICE_URL`, `REGEX_TIMEOUT` (vía
`app/config/settings.py`).

### Artifact store FS (D3)

Blobs grandes salen de Redis (`maxmemory 1gb + noeviction`) hacia FS local con
hash sharding en `pkg/worker_common/artifact_store.py` (`FSStore`, path
`data/{ab}/{cd}/{sha256}.bin`, 65k buckets, escritura atómica). Keys migradas:
`:text`, `:chunks`, `:embeddings`, `:inference_embeddings`, `:results`. En Redis
quedan solo refs `sha256:<hex>` + control/locks + `:micro_inferences_raw`.
Compat: un valor que NO empieza con `sha256:` se interpreta como payload legacy
(raw) — lectores usan `resolve()`/`resolve_text()`. Sin TTL en FS (limpieza GC
fuera de alcance). Volumen `artifacts-data` montado en `/app/data/artifacts`.

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
```

---

## Go Binary Convention

Todos los binarios Go se buildean en `bin/` en la raíz del proyecto. Nunca en el directorio del source.

### Regla única

```bash
go build -o bin/<nombre> <package>   # correcto
go build -o cmd/foo/foo ./cmd/foo    # INCORRECTO — nunca en el source
```

### Binarios del proyecto

| Target Makefile | Salida |
|-----------------|--------|
| `make build-orchestrator` | `bin/orchestrator` |
| `make build-resource-manager` | `bin/resource-manager` |
| `make build-client` | `bin/client` |
| `make build` | todos los anteriores |

`bin/*` y `tools/client/client` están en `.gitignore` — **nunca commitear binarios compilados**.

### Si un binario acaba trackeado en git por error

```bash
git rm --cached <ruta-del-binario>
# Añadir la ruta a .gitignore si no está ya cubierta
```

---

## RabbitMQ Queue Declaration (CRITICAL)

Toda declaración de cola RabbitMQ debe incluir **exactamente los mismos argumentos** en Go y Python.

### Regla

Si modificas `internal/broker/rabbitmq.go:declareQueue()`, debes actualizar **todos** estos archivos en el mismo commit:

| Archivo | Función |
|---------|---------|
| `internal/broker/rabbitmq.go` | `declareQueue()` — Go orchestrator |
| `pkg/worker_common/base.py` | `BaseWorker.run()` — embeddings, entities, metadata |
| `pkg/worker_common/async_base.py` | `BaseAsyncWorker.connect_rabbitmq()` — extraction, audio, image |
| `pkg/worker_common/rabbitmq.py` | `declare_queue()` — inference-worker, utilities |
| `pkg/worker_common/rabbitmq_async.py` | `declare_queue_async()` — extraction-worker |

### Args actuales

```python
arguments={
    "x-dead-letter-exchange": "document_processor_dlx",
    "x-dead-letter-routing-key": f"{queue_name}_failed",
}
```

### Síntomas de inconsistencia

- Workers entran en loop de reconexión con `PRECONDITION_FAILED`
- Los mensajes se acumulan en las colas (0 consumers)
- RabbitMQ logea: `inequivalent arg 'x-dead-letter-exchange'`
