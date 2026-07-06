# Plan: Fase 2 — Unificación de Workers Python Async

## Overview

Crear una clase base async `BaseAsyncWorker` en `pkg/worker_common/async_base.py` y migrar `extraction-worker`, `audio-worker` e `image-worker` a ella. El `completion-worker` queda como caso aparte: no usa RabbitMQ, usa Redis pub/sub; se abordará con una extensión de `BaseAsyncWorker` o como worker especializado separado.

**Objetivo:** eliminar la duplicación de conexión RabbitMQ, métricas, signal handling y publicación downstream en los workers async, al mismo tiempo que se preserva la lógica de negocio específica (Docling, Whisper, multimodal LLM).

## Arquitectura

```
pkg/worker_common/async_base.py
    ├── BaseAsyncWorker
    │       ├── redis_client / event_bus
    │       ├── metrics (Counter, Histogram)
    │       ├── health server (FastAPI + uvicorn)
    │       ├── connect_rabbitmq()  -> aio_pika robust + DLX queue
    │       ├── run()               -> reconexión + consume loop
    │       ├── process_message()   -> abstract
    │       ├── publish_downstream() -> publish a [embeddings|entities|metadata|inferences]
    │       └── _signal_stop()      -> asyncio.Event
    │
    cmd/extraction-worker/worker.py   (async process_message — Docling + exiftool + chunking)
    cmd/audio-worker/worker.py        (async process_message — Whisper via executor)
    cmd/image-worker/worker.py        (async process_message — LLM via executor)
```

## Decisiones de diseño

1. **Patrón de consumo unificado**: usar `queue.iterator()` + `asyncio.create_task()` (como extraction-worker) porque permite concurrencia controlada y limpieza de tareas pendientes en shutdown. Audio/image usan `queue.consume(callback)` simple; se migran al patrón iterator.

2. **DLX en todas las colas async**: audio/image declaran cola directamente sin DLX; extraction usa `declare_queue_async` con DLX. Se unifica: todos usan `declare_queue_async` con `x-dead-letter-exchange` y `x-dead-letter-routing-key`.

3. **Signal handling unificado**: `asyncio.Event` + `loop.add_signal_handler` (estilo extraction) en lugar de `register_signal_handlers(connection)` de audio/image. Más portable y no depende de API interna de `aio_pika`.

4. **Completion-worker**: fuera de `BaseAsyncWorker`. Se propone `BasePubSubWorker` o mantenerlo standalone. Se decide en tarea separada.

5. **pydantic_settings**: se recomienda usar en audio/image porque ya existe `app/config/settings.py`; en extraction se puede dejar `os.getenv` o también migrar. Se decide con el usuario.

6. **Shared chunking**: `audio-worker` e `image-worker` tienen chunking casi idéntico; se extrae a `pkg/worker_common/chunking.py` si el usuario acepta.

## Task List

### Phase 1: BaseAsyncWorker

- [ ] **Task 1.1: Crear `pkg/worker_common/async_base.py`**
  - Clase `BaseAsyncWorker` abstracta
  - `__init__(worker_name, queue_name, metrics_port, requires_gpu=False)`
  - Redis client, EventBus, métricas `jobs_total` y `job_duration`
  - Health server FastAPI con `/health`, `/ready`, `/metrics` (igual que `BaseWorker`)
  - Signal handlers con `asyncio.Event`
  - `connect_rabbitmq()` con `aio_pika.connect_robust`, DLX queue, `set_qos`
  - `publish_downstream(channel, queues, message)` helper
  - `run()` con bucle de reconexión y graceful shutdown
  - `process_message(message, channel)` abstracto

  **Files:** `pkg/worker_common/async_base.py`

  **Acceptance criteria:**
  - Compiles sin errores de sintaxis (`python -m py_compile`)
  - No rompe imports existentes
  - Health server arranca en puerto `metrics_port`

- [ ] **Task 1.2: Extraer chunking simple compartido (opcional)**
  - Mover chunking simple de audio/image a `pkg/worker_common/chunking.py`

  **Files:** `pkg/worker_common/chunking.py`, `cmd/audio-worker/segment_chunker.py`, `cmd/image-worker/worker.py`

  **Acceptance criteria:**
  - `chunk_text(text, chunk_size, overlap)` disponible para audio/image/extraction fallback
  - Tests existentes de audio-worker/image-worker no cambian (salvo mocks si es necesario)

### Checkpoint 1
- [ ] `BaseAsyncWorker` se instancia en un script de prueba mínimo sin fallos
- [ ] Docker build de `pkg/worker_common` no se rompe

---

### Phase 2: Migrar audio-worker

- [ ] **Task 2.1: Migrar `cmd/audio-worker/worker.py` a `BaseAsyncWorker`**
  - Heredar de `BaseAsyncWorker`
  - Eliminar `jobs_total`, `job_duration`, `start_http_server`, `register_signal_handlers`
  - Implementar `async process_message(message, channel)`
  - Preservar `WhisperClientPool` vía `loop.run_in_executor`
  - Preservar `SegmentChunker` y lógica de audio
  - Usar `publish_downstream()` para embeddings/entities/metadata/inferences

  **Files:** `cmd/audio-worker/worker.py`

  **Acceptance criteria:**
  - No queda código duplicado de RabbitMQ/métricas/signals
  - Entry point `async def main()` llama `worker.run()`
  - Validación de `document_path` y `MAX_AUDIO_SIZE_MB` se mantiene

- [ ] **Task 2.2: Verificar Dockerfile de audio-worker**
  - Entry point sigue siendo `python worker.py`
  - `pkg/worker_common/async_base.py` está en el path

  **Files:** `cmd/audio-worker/Dockerfile`

### Phase 3: Migrar image-worker

- [ ] **Task 3.1: Migrar `cmd/image-worker/worker.py` a `BaseAsyncWorker`**
  - Heredar de `BaseAsyncWorker`
  - Eliminar duplicación de RabbitMQ/métricas/signals
  - Implementar `async process_message(message, channel)`
  - Preservar `MultimodalLLMClientPool` vía executor
  - Usar `publish_downstream()`

  **Files:** `cmd/image-worker/worker.py`

- [ ] **Task 3.2: Verificar Dockerfile de image-worker**
  - Entry point sigue siendo `python worker.py`

  **Files:** `cmd/image-worker/Dockerfile`

### Phase 4: Migrar extraction-worker

- [ ] **Task 4.1: Migrar `cmd/extraction-worker/worker.py` a `BaseAsyncWorker`**
  - Heredar de `BaseAsyncWorker`
  - Reemplazar loop principal manual por `worker.run()`
  - Eliminar duplicación de métricas y signal handling
  - Implementar `async process_message(message, channel)`
  - Preservar Docling API, exiftool, chunking, source classification
  - Preservar routing especial para spreadsheets

  **Files:** `cmd/extraction-worker/worker.py`

- [ ] **Task 4.2: Verificar Dockerfile de extraction-worker**

  **Files:** `cmd/extraction-worker/Dockerfile`

### Checkpoint 2
- [ ] Los 3 workers arrancan sin errores de importación
- [ ] `docker-compose` levanta extraction/audio/image workers
- [ ] Métricas Prometheus accesibles en puertos correspondientes

---

### Phase 5: Completion-worker

- [ ] **Task 5.1: Decidir approach**
  - Opción A: crear `BasePubSubWorker` en `pkg/worker_common/pubsub_base.py`
  - Opción B: refactorizar `CompletionWorker` standalone para compartir solo metrics/signals/Redis/health sin RabbitMQ
  - **Pregunta al usuario:** ¿preferís A, B, o dejar completion como está por ahora?

- [ ] **Task 5.2: Implementar approach elegido**

  **Files:** depende de la opción

### Phase 6: Cleanup y verificación

- [ ] **Task 6.1: Actualizar `pkg/worker_common/example_worker.py`**
  - Añadir ejemplo async usando `BaseAsyncWorker`

- [ ] **Task 6.2: Verificar docker-compose**
  - `docker-compose up extraction-worker audio-worker image-worker completion-worker` levanta todo

- [ ] **Task 6.3: Revisar imports en `pkg/worker_common/security.py`**
  - `register_signal_handlers` ya no se usa por audio/image; decidir si se mantiene por compatibilidad

- [ ] **Task 6.4: Actualizar spec y plan**
  - `.opencode/plans/2026-07-02-async-workers-unification-plan.md`
  - `session-notes.md`

## Riesgos y mitigaciones

| Risk | Impact | Mitigation |
|------|--------|------------|
| Cambiar patrón de consume en audio/image rompe el orden de mensajes | Medio | `queue.iterator()` respeta QoS y prefetch; se mantiene `auto_ack=False` |
| DLX en audio/image cambia comportamiento de reintentos | Medio | Los workers ya heredan de orchestrator la topología DLX; unificar es correcto |
| Whisper/LLM bloquean el event loop si no se envuelven en executor | Alto | `BaseAsyncWorker` provee helper `run_in_executor()` |
| `completion-worker` refactor es grande y arriesgado | Medio | Se aborda en tarea separada, con aprobación del usuario |
| Docker builds fallan por nuevo archivo en `pkg/worker_common` | Bajo | Verificar que todos los Dockerfiles copian `pkg/` |

## Open Questions

1. **¿Usamos pydantic_settings en audio/image/extraction?** Audio/image ya tienen `app/config/settings.py` sin usar. Extraction no lo usa. ¿Querés que unifiquemos configuración con pydantic_settings o mantenemos `os.getenv`?

2. **¿Extraemos el chunking simple compartido?** Audio e image tienen chunking casi idéntico. ¿Lo centralizamos en `pkg/worker_common/chunking.py`?

3. **¿Qué hacemos con `completion-worker`?**
   - Opción A: `BasePubSubWorker` nuevo
   - Opción B: refactor standalone con helpers compartidos
   - Opción C: dejarlo fuera de esta fase

4. **¿Tests unitarios o solo e2e?** Según la sesión anterior, nos saltamos tests Python. ¿Confirmamos que para fase 2 tampoco arreglamos tests unitarios y validamos solo con el cliente e2e?

5. **¿El `extraction-worker` debe migrar a pydantic_settings?** Tiene muchas variables de entorno; pydantic_settings reduciría errores tipográficos.

## Verification Commands

```bash
# Build de imágenes
docker build -t textflow-extraction -f cmd/extraction-worker/Dockerfile .
docker build -t textflow-audio -f cmd/audio-worker/Dockerfile .
docker build -t textflow-image -f cmd/image-worker/Dockerfile .

# Docker compose
docker-compose -f deploy/docker/docker-compose.yml up extraction-worker audio-worker image-worker

# Verificar métricas
curl http://localhost:8004/metrics  # extraction
curl http://localhost:8005/metrics  # audio
curl http://localhost:8006/metrics  # image
```
