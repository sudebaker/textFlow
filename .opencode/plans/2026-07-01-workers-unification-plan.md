# Plan: Unificación de Python Workers sobre BaseWorker

## Overview

Migrar los 4 workers Python que usan RabbitMQ con pika blocking (`embeddings`, `entities`, `metadata`, `inference`) a `BaseWorker` como clase base, eliminando ~400 líneas de código duplicado y normalizando retry, metrics y señal handling.

**Workers fuera de scope**: `extraction-worker`, `audio-worker`, `image-worker` (usan `aio_pika` async) y `completion-worker` (Redis pub/sub sin RabbitMQ). Estos requieren un `BaseAsyncWorker` separate que es scope de un proyecto futuro.

## Architecture Decisions

1. **API unificada**: `BaseWorker.process_message(message: Dict)` — los workers migrados implementan solo este método. El signature actual `process(ch, method, properties, body)` se elimina.
2. **Métricas centralizadas**: `BaseWorker` ya define `jobs_total`, `job_duration`, `gpu_available`. Workers migrated ya no definen sus propias métricas.
3. **Retry centralizado**: `_get_retry_count` y `_should_retry` eliminados de cada worker — `BaseWorker._on_message` los usa internamente.
4. **Service injection**: cada worker recibe su servicio (EmbeddingService, NERService, etc.) en `__init__` via constructor, no como singleton global.
5. **Entry point**: `worker.py` se mantiene como entry point (no `main.py`).

## Task List

### Phase 1: Migrate metadata-worker (XS — el más simple, sin ML)

- [ ] Task 1.1: Crear `MetadataWorker(BaseWorker)` en `cmd/metadata-worker/worker.py`
- [ ] Task 1.2: Eliminar métricas propias, retry propio, signal handler propio
- [ ] Task 1.3: Verificar que `make run-workers` (o docker-compose) levanta el worker
- [ ] Task 1.4: Tests: `pytest cmd/metadata-worker/tests -v`

**Acceptance criteria:**
- `metadata-worker/worker.py` hereda de `BaseWorker`
- No redefine `jobs_total`, `job_duration`, `gpu_available`
- No redefine `_get_retry_count` ni `_should_retry`
- Signal handling, graceful shutdown, health check heredados de `BaseWorker`
- `process_message(self, message: Dict)` es el único método de negocio

**Files touched:** `cmd/metadata-worker/worker.py`

**Dependencies:** Ninguna

---

### Phase 2: Migrate embeddings-worker

- [ ] Task 2.1: Crear `EmbeddingsWorker(BaseWorker)` con `EmbeddingService` inyectado
- [ ] Task 2.2: Reimplementar `process_message()` usando lógica existente de `process()`
- [ ] Task 2.3: Eliminar métricas propias, retry propio, signal handler propio
- [ ] Task 2.4: Tests: `pytest cmd/embeddings-worker/tests -v`
- [ ] Task 2.5: Verificar batch processing y GPU detection

**Acceptance criteria:**
- `embeddings-worker/worker.py` hereda de `BaseWorker`
- No redefine métricas, retry, ni signal handling
- `process_message(message)` extrae chunks de Redis/msg, llama `service.generate_embeddings()`, guarda en Redis, publica EventBus progress
- Batching adaptativo GPU/CPU preservado
- Tests pasan con `HF_HUB_OFFLINE=1`

**Files touched:** `cmd/embeddings-worker/worker.py`

**Dependencies:** Task 1 (MetadataWorker migrado como referencia de patrón)

---

### Phase 3: Migrate entities-worker

- [ ] Task 3.1: Crear `EntitiesWorker(BaseWorker)` con `NERService` inyectado
- [ ] Task 3.2: Reimplementar `process_message()` — GLiNER batching + sliding window
- [ ] Task 3.3: Eliminar métricas propias y retry duplicado
- [ ] Task 3.4: Tests: `pytest cmd/entities-worker/tests -v`
- [ ] Task 3.5: Offline NER diagnosis: `python cmd/entities-worker/offline_diagnosis.py`

**Acceptance criteria:**
- `entities-worker/worker.py` hereda de `BaseWorker`
- GLiNER batching para chunks pequeños, sliding window para chunks grandes
- Deduplicación fuzzy de entidades preservada
- Entidades almacenadas en Redis como `inferences`
- Publicación a cola `inferences` preservada

**Files touched:** `cmd/entities-worker/worker.py`

**Dependencies:** Task 2

---

### Phase 4: Migrate inference-worker

- [ ] Task 4.1: Crear `InferenceWorker(BaseWorker)` con vLLM client inyectado
- [ ] Task 4.2: Reimplementar `process_message()` — batch buffer + assembly lock
- [ ] Task 4.3: Eliminar métricas propias y retry duplicado
- [ ] Task 4.4: Tests: verificar batch assembly y Redis SETNX lock
- [ ] Task 4.5: Tests: `pytest cmd/inference-worker/tests -v`

**Acceptance criteria:**
- `inference-worker/worker.py` hereda de `BaseWorker`
- Batch buffer (BATCH_SIZE=3, timeout=500ms) preservado
- Assembly lock via SETNX cuando `remaining=0` preservado
- Fallback individual en batch failure preservado
- Redis cache preservado

**Files touched:** `cmd/inference-worker/worker.py`

**Dependencies:** Task 3

---

### Phase 5: Verify y Clean

- [ ] Task 5.1: `docker-compose up` levanta todos los workers migrados
- [ ] Task 5.2: `make test-python` pasa
- [ ] Task 5.3: Eliminar código duplicado residual (`pkg/worker_common/rabbitmq.py` funciones residuales)
- [ ] Task 5.4: Actualizar `pkg/worker_common/example_worker.py` como referencia

**Acceptance criteria:**
- Todos los workers migrados levantan con `docker-compose up`
- Métricas Prometheus accesibles en cada worker port
- Health check `/health` responde en cada worker port+1000
- `make test-python` pasa sin errores

**Files touched:** `docker-compose.yml`, `pkg/worker_common/rabbitmq.py`

**Dependencies:** Tasks 1-4

---

## Open Questions

1. ¿Los tests existentes cubren bien la lógica de negocio de cada worker, o hay huecos?
2. ¿Se quiere mantener backward compatibility con el signature `process(ch, method, properties, body)` durante la migración (deprecated), o se hace corte limpio?
3. ¿El `require_gpu` de `BaseWorker.__init__` se debe usar para detectar GPU automáticamente en vez del `detect_gpu()` propio de cada worker?

## Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Breaking changes en workers | High | Tests primero, mantener entry points iguales |
| Model loading en `__init__` vs lazy | Medium | Mantener lazy loading como está |
| Offline mode se pierde | High | Verificar `HF_HUB_OFFLINE=1` en docker-compose |

## Verification Commands

```bash
# Unit tests por worker
pytest cmd/metadata-worker/tests -v
pytest cmd/embeddings-worker/tests -v
pytest cmd/entities-worker/tests -v
pytest cmd/inference-worker/tests -v

# Integration (docker-compose)
docker-compose up embeddings-worker entities-worker metadata-worker inference-worker

# Python tests globales
make test-python
```
