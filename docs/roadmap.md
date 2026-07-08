# IA Text Orchestrator – Production Roadmap Plan

> **For Implementation:** Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Take the validated CPU pipeline to production quality: fix critical known issues, enable fully air-gapped Docling, support GPU acceleration, harden the system for reliability, and optimize end-to-end latency.

**Architecture:** Event-driven microservices (Go orchestrator + Python workers) connected via RabbitMQ and Redis. Docling handles PDF extraction; GLiNER extracts entities; BAAI/bge-m3 generates embeddings. All models are mounted from host volumes at runtime (air-gapped).

**Tech Stack:** Go/Gin (orchestrator), Python 3.11 (workers), GLiNER, BAAI/bge-m3, Docling-serve, RabbitMQ, Redis, Docker Compose, pytest, go test

---

## Phase A — Critical Bug Fixes (no infrastructure changes needed)

### Task A1: Remove DEBUG statements from orchestrator

**Files:**
- Modify: `cmd/orchestrator/main.go:359,365`

**Step 1: Read the context around the lines**

```bash
grep -n "fmt.Fprintf(os.Stderr" cmd/orchestrator/main.go
```
Expected output: two lines (~359 and ~365) with `DEBUG:` prefix.

**Step 2: Remove both `fmt.Fprintf` DEBUG lines**

Remove lines 359 and 365 (both `fmt.Fprintf(os.Stderr, "DEBUG: ...")`).

Also check whether `os` and `fmt` imports are still needed after removal:
```bash
grep -n '"fmt"\|"os"' cmd/orchestrator/main.go
```
If `fmt` or `os` are no longer used elsewhere, remove them from the import block too.

**Step 3: Build to confirm no compile errors**

```bash
make build
```
Expected: exits 0, binaries created in `bin/`.

**Step 4: Run Go tests**

```bash
make test
```
Expected: all tests pass (or same failures as before).

**Step 5: Commit**

```bash
git add cmd/orchestrator/main.go
git commit -m "fix: remove debug fprintf statements from getJobHandler"
```

---

### Task A2: Fix entity deduplication threshold and date/money thresholds

The entities-worker deduplication is too aggressive (blocks valid entities). The date and money thresholds in `.env.example` are wrong vs AGENTS.md.

**Files:**
- Modify: `deploy/docker/docker-compose.yml` (entities-worker env block)
- Modify: `.env.example`

**Step 1: Update docker-compose entities-worker env block**

In `deploy/docker/docker-compose.yml`, inside the `entities-worker` service `environment:` block, ensure these values are set:

```yaml
- DEDUPLICATION_ENABLED=false
- GLINER_DATE_THRESHOLD=0.45
- GLINER_MONEY_THRESHOLD=0.55
```

Verify current state first:
```bash
grep -A 30 "entities-worker:" deploy/docker/docker-compose.yml | grep -E "DEDUP|DATE|MONEY"
```

**Step 2: Update .env.example to document correct recommended values**

Add/update the comments for `ENTITY_THRESHOLD_DATE` and `ENTITY_THRESHOLD_MONEY` to show the correct recommended values (`0.45` / `0.55`), matching AGENTS.md.

**Step 3: Write a test to validate configuration is applied**

```bash
# Verify docker-compose renders correctly
docker compose -f deploy/docker/docker-compose.yml config | grep -A 40 "entities-worker" | grep -E "DEDUP|DATE|MONEY"
```

Expected output:
```
DEDUPLICATION_ENABLED: "false"
GLINER_DATE_THRESHOLD: "0.45"
GLINER_MONEY_THRESHOLD: "0.55"
```

**Step 4: Commit**

```bash
git add deploy/docker/docker-compose.yml .env.example
git commit -m "fix: disable deduplication, correct date/money thresholds for entities-worker"
```

---

### Task A3: Fix entities-worker unauthorized HuggingFace calls (offline enforcement)

**Problem:** GLiNER makes network calls to HF Hub even when `ALLOW_REMOTE_DOWNLOAD=false`. The Dockerfile explicitly avoids `HF_HUB_OFFLINE=1` because it breaks `model_info()` — but this leaves the production container able to make outbound HF calls.

**Files:**
- Read and understand: `cmd/entities-worker/worker.py` (GLiNER load section)
- Read and understand: `cmd/entities-worker/Dockerfile`
- Possibly modify: `cmd/entities-worker/worker.py`

**Step 1: Identify exact load call**

```bash
grep -n "from_pretrained\|GLiNER\|local_files_only\|HF_HUB\|ALLOW_REMOTE" cmd/entities-worker/worker.py | head -30
```

**Step 2: Verify the offline model path exists and is populated**

```bash
ls -la models/gliner_model/
```
Expected: directory with model files (config.json, pytorch_model.bin or safetensors, tokenizer files, etc.)

**Step 3: Test offline enforcement**

```bash
docker build -t entities-worker-test -f cmd/entities-worker/Dockerfile .
docker run --network=none -e GLINER_MODEL_PATH=/models/gliner_model \
  -v $(pwd)/models:/models \
  --rm entities-worker-test python -c "
from gliner import GLiNER
import os
os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['TRANSFORMERS_OFFLINE'] = '1'
model = GLiNER.from_pretrained('/models/gliner_model', local_files_only=True)
print('OFFLINE LOAD OK')
"
```
Expected: `OFFLINE LOAD OK` — if it fails with a network error, proceed to Step 4.

**Step 4: If Step 3 fails — patch the worker to set env vars before model load**

In `cmd/entities-worker/worker.py`, before the `GLiNER.from_pretrained(...)` call, add:

```python
import os
# Force offline mode: prevent any HF Hub network calls
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
```

Use `os.environ.setdefault` so it can still be overridden during development.

**Step 5: Re-run Step 3 to confirm offline load works**

**Step 6: Update docker-compose to set the offline vars for entities-worker**

In `deploy/docker/docker-compose.yml` entities-worker `environment:` block add:
```yaml
- HF_HUB_OFFLINE=1
- TRANSFORMERS_OFFLINE=1
```

**Step 7: Commit**

```bash
git add cmd/entities-worker/worker.py deploy/docker/docker-compose.yml
git commit -m "fix: enforce HF offline mode in entities-worker to prevent unauthorized HF Hub calls"
```

---

## Phase B — Air-Gapped Docling Model Packaging

**Goal:** Package Docling models locally so the container never needs internet access at runtime. Currently Docling downloads its own models on first use.

### Task B1: Download Docling models to host

**Step 1: Check if docling-tools CLI is available**

```bash
docker run --rm quay.io/docling-project/docling-serve:latest docling-tools --help 2>/dev/null || \
docker run --rm quay.io/docling-project/docling-serve:latest python -m docling.utils.export --help 2>/dev/null || \
echo "Need different approach"
```

**Step 2: Download models using docling-serve container**

```bash
mkdir -p models/docling
docker run --rm \
  -v $(pwd)/models/docling:/models/docling \
  quay.io/docling-project/docling-serve:latest \
  docling-tools models download -o /models/docling
```

If `docling-tools` is not available, use the Python API:
```bash
docker run --rm \
  -v $(pwd)/models/docling:/models/docling \
  quay.io/docling-project/docling-serve:latest \
  python -c "
from docling.models.base_ocr_model import BaseOcrModel
from docling.pipeline.standard_pdf_pipeline import StandardPdfPipeline
pipeline = StandardPdfPipeline.download_models_hf(local_dir='/models/docling')
print('Download complete')
"
```

**Step 3: Verify model files are present**

```bash
ls -lh models/docling/
du -sh models/docling/
```
Expected: several GB of model files (layout model, OCR model, table model, etc.)

**Step 4: Note the exact size and file list**

Record in commit message.

---

### Task B2: Configure Docling to use local models

**Files:**
- Modify: `deploy/docker/docker-compose.yml` (docling service)

**Step 1: Update docling service in docker-compose.yml**

Add volume mount and environment variables to the `docling:` service block:

```yaml
docling:
  image: quay.io/docling-project/docling-serve:latest
  container_name: ia-text-docling
  ports:
    - 8000:5001
  environment:
    - DOCLING_DEVICE=${DOCLING_DEVICE:-auto}
    - DOCLING_NUM_THREADS=${DOCLING_NUM_THREADS:-4}
    - DOCLING_SERVE_ARTIFACTS_PATH=/models/docling   # <-- NEW
    - HF_HUB_OFFLINE=1                               # <-- NEW
    - TRANSFORMERS_OFFLINE=1                          # <-- NEW
    - LOG_LEVEL=info
  volumes:
    - ../../models/docling:/models/docling:ro         # <-- NEW
  deploy:
    resources:
      limits:
        memory: 16G
      reservations:
        memory: 12G
  healthcheck:
    test: ["CMD", "curl", "-f", "http://localhost:5001/openapi.json"]
    interval: 10s
    timeout: 5s
    retries: 10
    start_period: 30s
  networks:
    - backend
```

**Step 2: Validate docker-compose syntax**

```bash
docker compose -f deploy/docker/docker-compose.yml config > /dev/null && echo "OK"
```

Expected: `OK`

**Step 3: Test Docling starts with local models**

```bash
docker compose -f deploy/docker/docker-compose.yml up docling -d
sleep 30
curl -f http://localhost:8000/openapi.json > /dev/null && echo "Docling healthy"
```

Expected: `Docling healthy`

**Step 4: Test offline — run with `--network=none` equivalent**

```bash
# Check no outbound calls during a conversion
docker run --rm --network=none \
  -v $(pwd)/models/docling:/models/docling:ro \
  -e DOCLING_SERVE_ARTIFACTS_PATH=/models/docling \
  -e HF_HUB_OFFLINE=1 \
  -e TRANSFORMERS_OFFLINE=1 \
  quay.io/docling-project/docling-serve:latest \
  python -c "print('container starts offline OK')"
```

**Step 5: Run full pipeline smoke test**

```bash
# Ensure all services up
docker compose -f deploy/docker/docker-compose.yml up -d

# Upload test PDF
JOB_ID=$(curl -s -X POST http://localhost:8080/v1/documents/upload \
  -F "file=@/tmp/simple_test.pdf" | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")

echo "Job ID: $JOB_ID"
sleep 120
curl -s http://localhost:8080/v1/documents/$JOB_ID | python3 -m json.tool | head -40
```

Expected: status `completed`, entities and embeddings present.

**Step 6: Commit**

```bash
git add deploy/docker/docker-compose.yml
git commit -m "feat: configure Docling for air-gapped mode with local model mount"
```

---

### Task B3: Add `.gitignore` entry for Docling models

The `models/docling/` directory will be several GB — it must not be committed.

**Files:**
- Modify: `.gitignore`

**Step 1: Check current .gitignore**

```bash
grep "models" .gitignore
```

**Step 2: Add entry if not already present**

```
# Docling models (downloaded at deployment time)
models/docling/
```

**Step 3: Verify no model files are staged**

```bash
git status models/
```

Expected: `models/docling/` shows as ignored or untracked but NOT staged.

**Step 4: Commit**

```bash
git add .gitignore
git commit -m "chore: gitignore Docling model directory"
```

---

## Phase C — GPU Support

**Goal:** Enable GPU acceleration for Docling (CUDA), embeddings-worker, and entities-worker. Reduce end-to-end processing time from ~23 min (CPU) to ~2-3 min (GPU).

> **Note:** This phase requires a machine with an NVIDIA GPU and Docker GPU support installed. Steps include conditional setup — if running on the current CPU-only machine, the docker-compose changes can be prepared and committed, but the full test (Steps 3-5) must be run on the GPU machine.

### Task C1: Update docker-compose.yml for GPU services

**Files:**
- Modify: `deploy/docker/docker-compose.yml`

**Step 1: Add GPU reservation to Docling service**

Replace the `deploy:` block for the `docling` service:

```yaml
docling:
  image: quay.io/docling-project/docling-serve:latest-cuda12   # CUDA variant
  environment:
    - DOCLING_DEVICE=${DOCLING_DEVICE:-cuda:0}                 # Default to GPU
    - DOCLING_NUM_THREADS=${DOCLING_NUM_THREADS:-4}
    - DOCLING_SERVE_ARTIFACTS_PATH=/models/docling
    - HF_HUB_OFFLINE=1
    - TRANSFORMERS_OFFLINE=1
    - LOG_LEVEL=info
  deploy:
    resources:
      reservations:
        devices:
          - driver: nvidia
            count: 1
            capabilities: [gpu]
      limits:
        memory: 16G
      # No memory reservation needed — GPU VRAM is separate
```

**Step 2: Add GPU reservation to embeddings-worker**

```yaml
embeddings-worker:
  ...
  deploy:
    resources:
      reservations:
        devices:
          - driver: nvidia
            count: 1
            capabilities: [gpu]
      limits:
        cpus: '4'
        memory: 6G
```

**Step 3: Add GPU reservation to entities-worker**

```yaml
entities-worker:
  ...
  deploy:
    resources:
      reservations:
        devices:
          - driver: nvidia
            count: 1
            capabilities: [gpu]
      limits:
        memory: 6G
```

**Step 4: Validate docker-compose syntax**

```bash
docker compose -f deploy/docker/docker-compose.yml config > /dev/null && echo "OK"
```

**Step 5: Commit (even if GPU machine not yet available)**

```bash
git add deploy/docker/docker-compose.yml
git commit -m "feat: add GPU device reservations for docling, embeddings-worker, entities-worker"
```

---

### Task C2: Update embeddings-worker Dockerfile for CUDA PyTorch

**Files:**
- Modify: `cmd/embeddings-worker/Dockerfile`
- Modify: `cmd/embeddings-worker/requirements.txt`

**Step 1: Enable the CUDA PyTorch install in Dockerfile**

In `cmd/embeddings-worker/Dockerfile`, the `pip install torch` line is commented out. Uncomment it and make it conditional:

```dockerfile
# Install PyTorch: CUDA if available, CPU fallback
ARG CUDA_VERSION=cu118
RUN pip install --no-cache-dir \
    torch torchvision torchaudio \
    --index-url https://download.pytorch.org/whl/${CUDA_VERSION} \
    || pip install --no-cache-dir torch torchvision torchaudio
```

**Step 2: Build and verify CUDA detection**

```bash
docker build -t embeddings-worker-gpu -f cmd/embeddings-worker/Dockerfile .
docker run --rm --gpus all embeddings-worker-gpu python -c "
import torch
print('CUDA available:', torch.cuda.is_available())
print('Device count:', torch.cuda.device_count())
"
```

Expected (on GPU machine): `CUDA available: True`

**Step 3: Commit**

```bash
git add cmd/embeddings-worker/Dockerfile
git commit -m "feat: enable CUDA PyTorch build for embeddings-worker GPU acceleration"
```

---

### Task C3: Validate GPU pipeline end-to-end

> **Must run on GPU machine.**

**Step 1: Check GPU availability**

```bash
nvidia-smi
docker run --rm --gpus all nvidia/cuda:12.0-base-ubuntu22.04 nvidia-smi
```

**Step 2: Pull CUDA Docling image**

```bash
docker pull quay.io/docling-project/docling-serve:latest-cuda12
```

**Step 3: Start full stack**

```bash
docker compose -f deploy/docker/docker-compose.yml up -d
docker compose -f deploy/docker/docker-compose.yml ps
```

**Step 4: Test with large PDF (up to 50 MB)**

```bash
# Generate or use a real multi-page PDF
JOB_ID=$(curl -s -X POST http://localhost:8080/v1/documents/upload \
  -F "file=@/path/to/large.pdf" | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")

# Monitor progress (check every 30s)
watch -n 30 "curl -s http://localhost:8080/v1/documents/$JOB_ID | python3 -c \"import sys,json; d=json.load(sys.stdin); print(d.get('status'), d.get('chunks_count', '-'))\""
```

Expected: completes within 3-5 minutes for a 20-page PDF.

**Step 5: Record benchmark results**

Update `PIPELINE_VALIDATION.md` with GPU results.

---

### Task C4: Update entities-worker Dockerfile for CUDA

**Files:**
- Modify: `cmd/entities-worker/Dockerfile`

**Step 1: Add CUDA-enabled PyTorch install**

```dockerfile
# Install PyTorch with CUDA support
ARG CUDA_VERSION=cu118
RUN pip install --no-cache-dir \
    torch \
    --index-url https://download.pytorch.org/whl/${CUDA_VERSION} \
    || pip install --no-cache-dir torch
```

**Step 2: Build and test**

```bash
docker build -t entities-worker-gpu -f cmd/entities-worker/Dockerfile .
docker run --rm --gpus all \
  -v $(pwd)/models:/models \
  -e GLINER_MODEL_PATH=/models/gliner_model \
  entities-worker-gpu python -c "
import torch
from gliner import GLiNER
print('CUDA available:', torch.cuda.is_available())
model = GLiNER.from_pretrained('/models/gliner_model', local_files_only=True)
print('GLiNER loaded on:', next(model.model.parameters()).device)
"
```

**Step 3: Commit**

```bash
git add cmd/entities-worker/Dockerfile
git commit -m "feat: enable CUDA PyTorch build for entities-worker GPU acceleration"
```

---

## Phase D — Production Hardening

### Task D1: Add retry logic and timeout handling to workers

**Problem:** Workers currently fail permanently on transient errors (network blips, Redis unavailable briefly). There is no retry mechanism at the worker level.

**Files:**
- Modify: `pkg/worker_common/base.py`
- Modify: `cmd/extraction-worker/worker.py`

**Step 1: Read the base worker**

```bash
cat pkg/worker_common/base.py
```

**Step 2: Write a failing test for retry behavior**

In `pkg/worker_common/tests/test_retry.py` (create if needed):

```python
import pytest
from unittest.mock import patch, MagicMock

def test_process_message_retried_on_transient_error():
    """Worker should retry process_message up to MAX_RETRIES on transient errors."""
    from pkg.worker_common.base import BaseWorker
    worker = MagicMock(spec=BaseWorker)
    worker.max_retries = 3
    worker.process_message.side_effect = [ConnectionError("Redis down"), ConnectionError("Redis down"), {"ok": True}]
    # Call the retry wrapper
    result = worker._process_with_retry({"job_id": "test"})
    assert result == {"ok": True}
    assert worker.process_message.call_count == 3
```

**Step 3: Run test to confirm it fails**

```bash
pytest pkg/worker_common/tests/test_retry.py -v
```

Expected: FAIL (`AttributeError: _process_with_retry` not defined).

**Step 4: Implement retry wrapper in BaseWorker**

In `pkg/worker_common/base.py`, add:

```python
import time

MAX_RETRIES = int(os.environ.get("WORKER_MAX_RETRIES", "3"))
RETRY_DELAY = float(os.environ.get("WORKER_RETRY_DELAY", "2.0"))

def _process_with_retry(self, message: dict):
    last_error = None
    for attempt in range(self.max_retries):
        try:
            return self.process_message(message)
        except (ConnectionError, TimeoutError) as e:
            last_error = e
            wait = RETRY_DELAY * (2 ** attempt)  # exponential backoff
            logger.warning(f"Transient error on attempt {attempt+1}/{self.max_retries}: {e}. Retrying in {wait}s")
            time.sleep(wait)
    raise last_error
```

Replace the `process_message` call in the main consumption loop with `_process_with_retry`.

**Step 5: Run test to confirm it passes**

```bash
pytest pkg/worker_common/tests/test_retry.py -v
```

Expected: PASS.

**Step 6: Run all Python tests**

```bash
make test-python
```

Expected: all pass.

**Step 7: Commit**

```bash
git add pkg/worker_common/base.py pkg/worker_common/tests/test_retry.py
git commit -m "feat: add exponential backoff retry logic to BaseWorker for transient errors"
```

---

### Task D2: Add job timeout watchdog to orchestrator

**Problem:** Jobs can get stuck in `processing` state indefinitely if a worker crashes mid-job without sending a failure event.

**Files:**
- Read: `cmd/orchestrator/main.go`
- Read: `internal/redis/` (find SetJobStatus, GetJobStatus)
- Modify: `cmd/orchestrator/main.go` (add background goroutine)

**Step 1: Find existing job state management**

```bash
grep -rn "StatusProcessing\|SetJobStatus\|GetJobStatus" internal/redis/ internal/models/ | head -20
```

**Step 2: Write failing test for watchdog**

In `internal/pipeline/watchdog_test.go` (new file):

```go
func TestWatchdog_MarksStuckJobsFailed(t *testing.T) {
    // Set a job to processing state with a timestamp older than timeout
    // Run watchdog
    // Assert job is now in failed state
}
```

**Step 3: Implement watchdog goroutine**

A background goroutine that every 60 seconds:
1. Scans Redis for keys matching `orchestrator:job:*:status`
2. For any job in `processing` state where `created_at` is older than `JOB_TIMEOUT` (default 30 min), sets status to `failed` with error `"job_timeout"`

**Step 4: Run tests**

```bash
make test
```

**Step 5: Commit**

```bash
git add cmd/orchestrator/main.go internal/pipeline/watchdog.go internal/pipeline/watchdog_test.go
git commit -m "feat: add job timeout watchdog to mark stuck processing jobs as failed"
```

---

### Task D3: Add Prometheus metrics to all workers

**Files:**
- Read: `pkg/metrics/` (Go metrics package)
- Modify: `cmd/embeddings-worker/worker.py`
- Modify: `cmd/entities-worker/worker.py`
- Modify: `cmd/extraction-worker/worker.py`

**Step 1: Check existing metrics instrumentation**

```bash
grep -rn "prometheus\|metrics\|Counter\|Histogram" cmd/embeddings-worker/ cmd/entities-worker/ cmd/extraction-worker/
grep -rn "METRICS_PORT" deploy/docker/docker-compose.yml
```

**Step 2: Identify which workers are missing key metrics**

Each worker should expose at minimum:
- `jobs_processed_total` (Counter, labels: `status=success|error`)
- `job_processing_duration_seconds` (Histogram)
- `model_load_time_seconds` (Gauge, set once at startup)

**Step 3: Add missing metrics per worker**

For each worker that is missing them, add after imports:

```python
from prometheus_client import Counter, Histogram, Gauge, start_http_server

JOBS_PROCESSED = Counter('jobs_processed_total', 'Total jobs processed', ['status'])
JOB_DURATION = Histogram('job_processing_duration_seconds', 'Job processing duration')
MODEL_LOAD_TIME = Gauge('model_load_time_seconds', 'Model load duration at startup')
```

Wrap `process_message` with `JOB_DURATION.time()` and increment `JOBS_PROCESSED`.

**Step 4: Verify metrics endpoint**

```bash
# Start worker (or use docker compose)
curl http://localhost:8001/metrics | grep jobs_processed
```

**Step 5: Commit**

```bash
git add cmd/embeddings-worker/worker.py cmd/entities-worker/worker.py cmd/extraction-worker/worker.py
git commit -m "feat: add prometheus metrics (counter, histogram) to all Python workers"
```

---

### Task D4: Integration test — test-e2e-complete.py cleanup and automation

**Files:**
- Read: `test-e2e-complete.py` (existing script)
- Modify: `test-e2e-complete.py`

**Step 1: Read the existing test script**

```bash
cat test-e2e-complete.py
```

**Step 2: Check what it currently validates**

Look for: upload, status polling, result assertions (chunks, embeddings, entities counts).

**Step 3: Add assertions that were missing from Phase 1 validation**

Ensure the script asserts:
- `status == "completed"`
- `len(embeddings) > 0`
- `len(entities) > 0`
- `len(chunks) > 0`
- Each embedding has exactly 1024 dimensions
- Each entity has: `text`, `label`, `score`, `start`, `end` fields

**Step 4: Add a timeout failure case**

```python
MAX_WAIT_SECONDS = 300
start = time.time()
while time.time() - start < MAX_WAIT_SECONDS:
    resp = requests.get(f"{BASE_URL}/v1/documents/{job_id}")
    if resp.json()["status"] in ("completed", "failed"):
        break
    time.sleep(10)
else:
    pytest.fail(f"Job did not complete within {MAX_WAIT_SECONDS}s")
```

**Step 5: Run the test end-to-end (requires docker compose up)**

```bash
python test-e2e-complete.py
```

Expected: all assertions pass.

**Step 6: Commit**

```bash
git add test-e2e-complete.py
git commit -m "test: harden e2e test script with explicit assertions and timeout handling"
```

---

### Task D5: Update PIPELINE_VALIDATION.md and roadmap.md

After each phase is complete, update documentation to reflect actual measured values:
- GPU benchmarks (when available)
- Air-gapped validation results
- Known issues resolved vs still open

**Step 1: After Phase A is done**

Update `PIPELINE_VALIDATION.md` section "Critical Issues Fixed" with:
- DEBUG statements removed (main.go)
- Deduplication disabled
- HF offline enforced

**Step 2: After Phase B is done**

Add section "Air-Gapped Docling" with:
- Model directory size
- Startup time with local models vs. first-run download

**Step 3: After Phase C is done (GPU machine)**

Add section "GPU Benchmarks" with a table:
| PDF Size | Pages | CPU Time | GPU Time | Speedup |

**Step 4: Commit after each update**

```bash
git add PIPELINE_VALIDATION.md roadmap.md
git commit -m "docs: update validation report with [phase] results"
```

---

## Phase E — Latency & Throughput Optimization

> **Objetivo:** Reducir latencia end-to-end del pipeline, eliminar cuellos de botella críticos y aumentar throughput sin degradar calidad de resultados.
>
> **Enfoque:** Optimizaciones incrementales con arquitectura estable. Sin reescrituras masivas. Prioridad por impacto/riesgo.

---

### Phase E.1 — Crítica: Jobs que nunca finalizan (Completion Worker Reconciliation)

**Problema:** El completion worker depende de Redis pub/sub (`job:events`) para detectar cuando un job está listo. Si se pierde un evento (worker reiniciado, desconexión de red), el job permanece en estado `processing` para siempre.

**Arquitectura actual:**
```
Workers → Redis pub/sub "job:events" → Completion Worker (pubsub.listen())
```

**Plan:** Añadir un **reconciliation loop** que escanee periódicamente jobs activos cuyos `steps` indican completitud pero cuyo estado sigue siendo `processing`.

#### Task E1.1: Implementar `ReconciliationService` en `cmd/completion-worker/`

- Crear `reconciliation_service.py` con un thread que cada 30 segundos:
  - `SCAN` o `ZRANGE` en `active_jobs` para jobs con `status=processing`
  - Para cada job, `HGETALL` en `:steps` y verificar si todos los steps requeridos están `completed`
  - Si está completo pero status != completed, invocar `finalize_job()` forzosamente
- Thread con `daemon=True`, arranca en `__init__` del worker

#### Task E1.2: Añadir métrica de "stale jobs rescued"

- Loguear + contador de cuántos jobs fueron rescatados por reconciliación

#### Task E1.3: Test de reconocimiento de job huérfano

- Simular job con steps completos pero status `processing`
- Verificar que reconciliación lo detecta y finaliza

---

### Phase E.2 — Alta: Embeddings de inferencias por batch global

**Problema:** Tanto `embeddings-worker` como `completion-worker` generan embeddings de inferencias **chunk por chunk**. Si un job tiene 100 chunks con 10 inferencias cada uno, hacen 100 llamadas a `model.encode()` en lugar de 1 llamada con 1000 textos.

**Plan:** Aplanar todos los textos de inferencias de todos los chunks en una sola lista, generar embeddings en un solo batch, luego remapear a chunks.

#### Task E2.1: Batch global en `embeddings-worker` (`_generate_inference_embeddings`)

Archivos:
- Modificar: `cmd/embeddings-worker/worker.py`

Actualizar `_generate_inference_embeddings` para:
- Recolectar TODOS los `(chunk_id, inference_text)` en una lista plana
- Llamar `model.encode(all_texts, batch_size=EMBEDDING_BATCH_SIZE)` una sola vez
- Remapear resultados de vuelta a `Dict[chunk_id, Dict[idx, embedding]]`
- Tiempo estimado: de O(N_chunks) a O(1) en llamadas a encode

#### Task E2.2: Batch global en `completion-worker` (`_generate_inference_embeddings`)

Archivos:
- Modificar: `cmd/completion-worker/worker.py`

- Misma refactorización que en embeddings-worker
- Eliminar la lógica de fallback que carga SentenceTransformer (ya no necesaria si embeddings-worker siempre genera)

#### Task E2.3: Tests de batch global

- Verificar que embeddings de inferencias con múltiples chunks producen resultados correctos
- Verificar que el orden y mapeo chunk→inference se preserva

---

### Phase E.3 — Alta: Conexión RabbitMQ reutilizada en Entities Worker

**Problema:** `entities-worker` abre una **nueva conexión `pika.BlockingConnection`** por cada job que publica inferencias. Esto implica TCP handshake + TLS + AMQP handshake + autenticación por job.

**Plan:** Reutilizar el canal del consumidor para publicar mensajes downstream.

#### Task E3.1: Refactorizar `entities-worker` para publisher reutilizado

Archivos:
- Modificar: `cmd/entities-worker/worker.py`

- En `worker.__init__`, crear un segundo canal sobre la misma conexión: `self._publisher_channel`
- En `process()`, usar `self._publisher_channel.basic_publish()` en lugar de `pika.BlockingConnection()`
- Añadir `try/except` para reconexión lazy si el canal se cierra

#### Task E3.2: Test de conexión reutilizada

- Verificar que tras procesar N jobs, solo existe 1 conexión activa
- Verificar que publicación sigue funcionando tras error transitorio

---

### Phase E.4 — Media-Alta: Pipeline Redis en Orchestrator

**Problema:** `getJobHandler` y `CreateBatchHandler` hacen múltiples lecturas/escrituras Redis secuenciales en lugar de pipelines.

**Plan:** Implementar métodos de pipeline en `internal/redis/client.go` y usarlos en handlers.

#### Task E4.1: Añadir `GetJobStatusPipelined()` en `internal/redis/client.go`

- Un solo `Pipeline()` que hace `HGet(status)` + `HGetAll(steps)` + `Get(error)` + `HGet(created_at)` + `HGet(webhook)`
- Retornar estructura `JobStatusSnapshot` consolidada

#### Task E4.2: Refactorizar `getJobHandler` en `cmd/orchestrator/main.go`

- Reemplazar 4 llamadas Redis secuenciales por una sola llamada a `GetJobStatusPipelined()`

#### Task E4.3: Añadir `CreateBatchPipelined()` en `internal/redis/client.go`

- Para batch creation: `Pipeline()` con `HSet(status)` + `HSet(created)` + `HSet(features)` + `HSet(batch_id)` + `SAdd(batch_jobs)` para cada job
- Reducir de ~5 RTT/job a 1 RTT/job

#### Task E4.4: Refactorizar `CreateBatchHandler` en `cmd/orchestrator/handlers/batch.go`

- Usar `CreateBatchPipelined()` dentro de cada goroutine

#### Task E4.5: Tests de pipeline

- Test que verifica que `GetJobStatusPipelined` retorna los mismos datos que las 4 llamadas individuales
- Benchmark opcional: medir RTT reducido

---

### Phase E.5 — Media: Deduplicación de entidades O(N²) → O(N)

**Problema:** `deduplicate_entities()` en `entities-worker` y `completion-worker` es O(N²) con `fuzz.ratio`. Para miles de entidades, esto es lento.

**Plan:** Usar un diccionario indexado por `(normalized_text + label)` como pre-filtro antes del fuzzy matching.

#### Task E5.1: Optimizar `deduplicate_entities()` en `cmd/entities-worker/worker.py`

Archivos:
- Modificar: `cmd/entities-worker/worker.py`

- Normalizar texto (lowercase, strip spaces)
- Crear dict `keyed = {(norm_text, label): entity}`
- Solo aplicar `fuzz.ratio` si hay colisiones de clave (mismo texto normalizado + label)
- Convierte O(N²) en O(N) para la mayoría de casos

#### Task E5.2: Optimizar `deduplicate_entities()` en `cmd/completion-worker/worker.py`

Archivos:
- Modificar: `cmd/completion-worker/worker.py`

- Misma refactorización que en entities-worker
- Evaluar si se puede eliminar completamente si entities-worker ya deduplica con threshold suficiente

#### Task E5.3: Tests de deduplicación

- Verificar que 1000 entidades con 500 duplicadas se procesan en <1s
- Verificar que resultado es idéntico al algoritmo O(N²) original

---

### Phase E.6 — Media: Worker Extraction — Prefetch y Regex

**Problema:**
- `EXTRACTION_CONCURRENCY=5` puede subutilizar el worker si Docling es rápido
- Regex patterns en `SourceClassifier` se compilan en cada llamada
- Regex en entities worker (`_extract_dates`, etc.) también se compilan en cada llamada

**Plan:** Aumentar prefetch y compilar regexes una sola vez.

#### Task E6.1: Compilar regexes en `SourceClassifier`

Archivos:
- Modificar: `cmd/extraction-worker/`

- Mover `PATTERNS` de strings a `re.compile()` a nivel de clase
- Actualizar `_classify_by_patterns()` para usar patterns compilados

#### Task E6.2: Compilar regexes en entities worker

Archivos:
- Modificar: `cmd/entities-worker/worker.py`

- Crear módulo-level compiled patterns para `_extract_dates`, `_extract_money`, `_extract_orgs`, `_extract_locs`, `_extract_persons`
- Usar `compiled_pattern.search()` en lugar de `re.search(pattern_string, ...)`

#### Task E6.3: Hacer `EXTRACTION_CONCURRENCY` configurable vía env var

- Default subir a 10 (desde 5)
- Añadir validación: `max(1, min(50, value))`

#### Task E6.4: Tests

- Verificar que SourceClassifier produce mismo resultado con patterns compilados
- Verificar que entities regex extractors producen mismos resultados

---

### Phase E.7 — Media-Baja: Infraestructura Docker y GPU

**Problema:** Múltiples workers GPU (`embeddings`, `entities`, `completion`, `docling`) comparten `CUDA_VISIBLE_DEVICES=0`, generando contención.

**Plan:** Estrategias de mitigación sin requerir hardware adicional.

#### Task E7.1: Docker Compose: MPS (Multi-Process Service) para GPU

Archivos:
- Modificar: `docker-compose.gpu.yml`

- Añadir `runtime: nvidia` con `capabilities: [gpu]` donde corresponda
- Añadir variable `NVIDIA_MPS_ACTIVE=1` para permitir sharing de contexto CUDA
- Documentar el cambio

#### Task E7.2: Reducir `PREFETCH_COUNT` de embeddings worker

- De 20 a 8 para evitar acumulación de mensajes que sature VRAM
- Añadir comentario explicativo en docker-compose

#### Task E7.3: Añadir `torch.cuda.empty_cache()` en workers GPU

- En `embeddings-worker`, después de cada job grande (>1000 chunks)
- En `entities-worker`, después de cada batch GLiNER
- En `completion-worker`, después de generar inference embeddings
- Controlable vía env var `CUDA_EMPTY_CACHE=true|false`

#### Task E7.4: Cambiar Redis `maxmemory-policy`

- En `docker-compose.yml`: cambiar de `noeviction` a `allkeys-lru`
- Esto previene rechazo de escrituras cuando Redis alcanza el límite de 1 GB

---

### Phase E.8 — Baja: RabbitMQ Pool y Rate Limiting

**Problema:** `RabbitMQPoolSize=5` puede ser insuficiente bajo alta carga de publicaciones concurrentes.

**Plan:** Escalar pool y añadir throttling interno.

#### Task E8.1: Aumentar `RabbitMQPoolSize` a 20

Archivos:
- Modificar: `internal/config/config.go`

- Cambiar default de 5 a 20
- Justificar con comentario: batch mode puede publicar N jobs simultáneamente

#### Task E8.2: Añadir `Semaphore` en workers Python para Docling/vLLM

- En `extraction-worker`: limitar a `MAX_DOCLING_CONCURRENT` (default 5)
- En `inference-worker`: limitar a `MAX_VLLM_CONCURRENT` (default 10)
- Evita saturar servicios externos bajo burst de jobs

#### Task E8.3: Tests de semáforo

- Simular 20 jobs concurrentes
- Verificar que solo N acceden a Docling/vLLM simultáneamente

---

### Phase E — Métricas de Validación

Antes y después de cada fase de optimización, medir:

| Métrica | Cómo medir | Target |
|---------|-----------|--------|
| Latencia job simple (PDF 10 páginas) | `time client -i doc.pdf -o out.json` | -30% |
| Latencia job con inferencias | `time client -i doc.pdf -o out.json -f` | -40% |
| Throughput batch (100 docs) | `time client -b batch.json -o out.json` | -25% |
| Jobs colgados (processing >1h) | `redis-cli ZRANGE active_jobs -inf +inf` | 0 |
| Redis RTT por poll | Log interno o `redis-cli --latency` | -60% |
| GPU VRAM usage | `nvidia-smi` durante carga | Sin OOM |

### Phase E — Orden de Implementación Recomendado

| Orden | Fase | Riesgo | Tiempo estimado | Impacto |
|-------|------|--------|-----------------|---------|
| 1 | E.1 (Reconciliación) | Bajo | 2-3h | Crítico |
| 2 | E.3 (RabbitMQ reuse) | Bajo | 1-2h | Alto |
| 3 | E.2 (Batch global embeddings) | Medio | 3-4h | Alto |
| 4 | E.4 (Redis pipeline) | Medio | 3-4h | Medio-Alto |
| 5 | E.5 (Deduplicación O(N)) | Bajo | 2h | Medio |
| 6 | E.6 (Regex + prefetch) | Bajo | 1-2h | Medio |
| 7 | E.7 (Infra MPS + cache) | Medio | 2-3h | Medio |
| 8 | E.8 (Pool + semáforos) | Bajo | 1-2h | Bajo |

**Tiempo total estimado:** ~15-20 horas de desarrollo + testing.

---

## Summary: Execution Order

| Phase | Tasks | Prerequisite | Machine |
|-------|-------|-------------|---------|
| **A** | A1, A2, A3 | None | Current (CPU) |
| **B** | B1, B2, B3 | Phase A complete | Current (CPU) |
| **C** | C1 (config) | Phase B complete | Current (CPU) |
| **C** | C2, C3, C4 (test) | C1 merged + GPU available | GPU machine |
| **D** | D1–D5 | Phase A complete (can run in parallel with B/C) | Current (CPU) |
| **E** | E1–E8 | Any phase (independent optimization track) | Current / GPU |

**Phases A and D can run in parallel.** Phase B requires Phase A complete. Phase C config can be done now; GPU testing requires the GPU machine. Phase E is an independent optimization track that can start after core pipeline stability is achieved.

## Estimated LOC

| Phase | Files Changed | Estimated LOC |
|-------|--------------|--------------|
| A | 3 files | ~15 lines removed/changed |
| B | 2 files + model download | ~20 lines changed |
| C | 3 Dockerfiles + docker-compose | ~40 lines |
| D | 5 files + 2 new test files | ~200 lines |
| E | ~12 files | ~400 lines |

## Verification

After all phases:

```bash
# Full system check
make test          # Go unit tests
make test-python   # Python unit tests
python test-e2e-complete.py  # E2E integration test

# Offline verification (no network)
docker run --network=none entities-worker python -c "from gliner import GLiNER; print('OK')"
docker run --network=none docling curl -f http://localhost:5001/openapi.json && echo "docling OK"
```
