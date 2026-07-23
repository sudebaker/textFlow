# Plan de Migración: Workers standalone → Bases compartidas

## Contexto

El merge `aa99d19` (22 Jul 2026) integró la rama `optimización-de-código` usando
`--allow-unrelated-histories -X theirs`. Esto sobreescribió 6 workers Python con
arquitectura "standalone" (sin herencia de `BaseWorker`/`BaseAsyncWorker`/`BasePubSubWorker`).

Los módulos `async_base.py`, `pubsub_base.py`, `chunking.py` quedaron orphaned en disco
— las bases existen pero ningún worker las importa (excepto `metadata-worker` que conservó
`BaseWorker`). HEAD (`d231138`) desciende del merge, no de la reversión.

**Decisión**: Opción B — volver a arquitectura de bases compartidas (estado funcional probado
en `e5e63c9`), sin tocar el broker Go (`internal/broker/` está saludable).

---

## Fase 0 — Preparación

```bash
# 1. Tag para rollback inmediato
git checkout -b recovery/bases-compartidas d231138
git tag pre-migration-snapshot

# 2. Baseline de tests
make test-python 2>&1 | tee /tmp/baseline-tests.txt
make test 2>&1 | tee /tmp/baseline-go-tests.txt

# 3. Baseline E2E (métrica de rendimiento pre-migración)
make docker-up
# Documento: tiempo extraction → completion
echo "Baseline E2E: subir documento, medir tiempo" > /tmp/baseline-e2e.txt
# Audio: tiempo whisper → completion
echo "Baseline E2E audio: subir .wav, medir tiempo" >> /tmp/baseline-e2e.txt
# Imagen: tiempo multimodal → completion
echo "Baseline E2E imagen: subir .jpg, medir tiempo" >> /tmp/baseline-e2e.txt
# Métricas Prometheus
docker exec textflow-orchestrator curl -s http://localhost:8080/metrics > /tmp/baseline-metrics.json
```

---

## Fase 1 — Migración

### 1.0 — Regresiones docker-compose

| Regresión | Causa | Fix |
|-----------|-------|-----|
| `INFERENCE_LLM_TIMEOUT` ausente en inference-worker | `aa99d19` eliminó la línea | Restaurar `INFERENCE_LLM_TIMEOUT=${INFERENCE_LLM_TIMEOUT:-300}` |
| Puerto `8081:8081` expuesto del regex-entity-extractor | `aa99d19` añadió `ports:` | Quitar el bloque `ports:` del regex-extractor |
| ~~Puerto orchestrator 9080→8080~~ | ~~aa99d19 cambió host port~~ | **ELIMINADO**: No hay conflicto real. Docling usa 5001 interno, no 8080. Cliente hardcodea `localhost:8080`. Mantener 8080. |

### 1.0a — Añadir hook `cleanup()` a `BaseWorker`

**Motivación**: Ninguna base libera GPU memory ni resources en shutdown. Las bases solo
setean flags. embeddings-worker y entities-worker cargan modelos CUDA sin liberarlos nunca.

```python
# BaseWorker._signal_handler (al final, antes de retornar)
def _signal_handler(self, signum, frame) -> None:
    self._shutdown_requested = True
    self._stopping = True
    self.cleanup()

def cleanup(self) -> None:
    """Hook para subclases. Libera resources antes de shutdown."""
    pass
```

`BaseAsyncWorker` y `BasePubSubWorker` también reciben el hook (no-op por defecto).

### 1.0b — Añadir `_parse_pubsub_message()` a `BasePubSubWorker`

**Motivación**: Simetría con `BaseWorker` (que pre-parsea `json.loads(body)` antes de
llamar `process_message`). Elimina lógica duplicada de filtrado + parseo en cada worker pubsub.

```python
def _parse_pubsub_message(self, message: Dict) -> Optional[Dict]:
    """Filtra mensajes de control y parsea el data JSON.
    Retorna None si no es mensaje de datos o el parseo falla.
    """
    if message.get("type") != "message":
        return None
    try:
        return json.loads(message["data"])
    except (json.JSONDecodeError, TypeError, KeyError) as e:
        logger.warning(f"Malformed pubsub message: {e}")
        return None
```

`start()` llama `_parse_pubsub_message(message)` antes de pasar a `handle_event(event)`.
El contrato cambia: `handle_event` recibe el **dict parseado** (simétrico a `process_message`).
El filtrado de `message["type"] != "message"` + `json.loads` viven en la base.

### 1.0c — Estandarizar logging en bases

**Motivación**: `pkg/logging_python.py` (setup_logging con JSON structured, zerolog-compatible)
existe pero solo `base.py` lo usa. `async_base.py` y `pubsub_base.py` usan `basicConfig` plano.

- `async_base.py`: reemplazar `logging.basicConfig(level=INFO, format=...)` + `getLogger("worker_common.async_base")` por `from pkg.logging_python import setup_logging; self.logger = setup_logging(worker_name)`
- `pubsub_base.py`: mismo cambio
- Resultado: los 3 bases emiten logs en mismo formato JSON. Workers migrados heredan logging
  sin necesidad de `basicConfig` propio.
- Migración elimina calls redundantes a `basicConfig` en `audio-worker` e `image-worker`
  (que llamaban `basicConfig` + `setup_logging`).

### 1.0d — Helper compartido: inference_embeddings.py

Extraer `_generate_inference_embeddings` (duplicada entre completion-worker y
embeddings-worker) a `pkg/worker_common/inference_embeddings.py`.

```python
# pkg/worker_common/inference_embeddings.py
def generate_inference_embeddings(
    redis_client, job_id: str, model, logger
) -> None:
    ...
```

Ambos workers importan `from pkg.worker_common.inference_embeddings import generate_inference_embeddings`.

### 1.1 — Rename de workers (4 archivos)

| Worker | Archivo actual | Archivo nuevo |
|--------|---------------|---------------|
| embeddings | `cmd/embeddings-worker/worker.py` | → `cmd/embeddings-worker/embeddings_worker.py` |
| entities | `cmd/entities-worker/worker.py` | → `cmd/entities-worker/entities_worker.py` |
| inference | `cmd/inference-worker/worker.py` | → `cmd/inference-worker/inference_worker.py` |
| completion | `cmd/completion-worker/worker.py` | → `cmd/completion-worker/completion_worker.py` |

Actualizar:
- `Dockerfile` en cada worker: `CMD ["python", "cmd/<worker>/<new_name>.py"]`
- `conftest.py` en cada worker (eliminar hooks Variant B, migrar a Variant A)
- `Makefile` si referencia paths
- Imports internos si referencian el módulo `worker`

### 1.2 — embeddings-worker → `BaseWorker`

**Contrato**: `class EmbeddingsWorker(BaseWorker)` → `process_message(message: Dict) -> Any`

**Eliminar** (delegado a la base):
- `connect_rabbitmq` manual (Pattern B → heredado)
- `signal_handler` duplicado
- `start_http_server` (Prometheus)
- FastAPI health endpoints
- `_get_retry_count`, `_should_retry` duplicados
- `basicConfig` propio (heredado de base)

**Preservar**:
- `load_model` (BAAI/bge-m3)
- GPU Gauges (`gpu_available`, `gpu_memory_gb`) — añadir en `__init__`
- `_load_micro_inferences`, `_save_inference_embeddings`
- `_generate_inference_embeddings` → reemplazar por `generate_inference_embeddings` del helper

**Implementar `cleanup()`**:
```python
def cleanup(self) -> None:
    super().cleanup()
    if hasattr(self, 'model'):
        del self.model
    import torch
    torch.cuda.empty_cache()
```

**Conftest**: copiar patrón Variant A de metadata-worker (mock pika/redis/prometheus/fastapi).

**Gate**: `pytest cmd/embeddings-worker/tests -v` + `pytest pkg/tests -v` + smoke E2E

**Commit**: `refactor(embeddings): migrate to BaseWorker, add GPU cleanup`

### 1.3 — entities-worker → `BaseWorker`

**Contrato**: `class EntitiesWorker(BaseWorker)` → `process_message(message: Dict) -> Any`

**Eliminar**:
- `connect_rabbitmq` manual
- `signal_handler` duplicado (definía el suyo propio que NO delegaba a la base)
- `start_http_server`, health
- `_get_retry_count`, `_should_retry` duplicados
- `basicConfig` propio

**Preservar**:
- `load_model` (GLiNER + regex extractors)
- `predict_entities`, `normalize_entity_text`, `deduplicate_entities`, `calculate_global_position`
- GPU Gauge (`gpu_available`)
- Regex extractors: `_extract_dates`, `_extract_money`, `_extract_orgs`, `_extract_locs`, `_extract_persons`

**Complicación**: entities abre un `pika.BlockingConnection` **interno** dentro de `process`
para publicar features de vuelta a la cola `inferences`. Solución:
reemplazar con `self._publish_to_queue("inferences", feature_msg)` — helper ya existente
en BaseWorker. Eliminar la conexión interna y el cleanup manual.

**Implementar `cleanup()`**:
```python
def cleanup(self) -> None:
    super().cleanup()
    if hasattr(self, 'model'):
        del self.model
    import torch
    torch.cuda.empty_cache()
```

**Conftest**: Variant A (mock pika/redis). Eliminar hooks de Variant B.

**Gate**: `pytest cmd/entities-worker/tests -v` + `pkg/tests` +
`docker run --network=none entities-worker python test_offline_ner.py` + smoke E2E

**Commit**: `refactor(entities): migrate to BaseWorker, use _publish_to_queue, add GPU cleanup`

### 1.4 — inference-worker → `BaseWorker` (batch custom)

**Contrato**: `class InferenceWorker(BaseWorker)` → `process_message(message: Dict) -> Any`

**Decisión**: Batch custom vía `_on_message_processed` hook + thread timer.
NO modificar BaseWorker para batch. Thread timer arranca como daemon.

**Eliminar**:
- `connect_rabbitmq` manual (Pattern B → heredado)
- `signal_handler` duplicado
- `start_http_server`, health
- `basicConfig` propio

**Preservar**:
- `_batch_buffer: List[Dict]` + `threading.Lock`
- `flush_batch_buffer`
- Thread timer para flush periódico (cada `BATCH_TIMEOUT_MS`)
- `_cache_key`, `_get_cached`, `_set_cached` (Redis cache)
- `extract_inferences` (single), `extract_inferences_batch`
- `_assemble_final_results`
- Redis `decr(remaining_counter)`
- Redis `setnx(assembly_lock_key)`

**Batch hook pattern con shutdown robusto**:
```python
def __init__(self):
    super().__init__(...)
    self._batch_buffer = []
    self._batch_lock = threading.Lock()
    self._timer_thread = None

def _start_batch_timer(self):
    if self._timer_thread and self._timer_thread.is_alive():
        return
    self._timer_thread = threading.Timer(
        self.batch_timeout_ms / 1000.0,
        self._flush_if_ready
    )
    self._timer_thread.daemon = True
    self._timer_thread.start()

def _flush_if_ready(self):
    if self._shutdown_requested:
        return
    with self._batch_lock:
        if len(self._batch_buffer) > 0:
            self._flush_batch_buffer()

def _on_message_processed(self) -> None:
    super()._on_message_processed()
    self._flush_if_ready()
    if not self._shutdown_requested:
        self._start_batch_timer()

def cleanup(self) -> None:
    """Flush final + join timer thread."""
    self._shutdown_requested = True
    if self._timer_thread and self._timer_thread.is_alive():
        self._timer_thread.join(timeout=2.0)
    # Flush mensajes pendientes
    with self._batch_lock:
        if len(self._batch_buffer) > 0:
            self._flush_batch_buffer()
```

**Conftest**: Variant A. Preservar mocks de LLM/Redis/cache.
**Test adicional**: `test_timer_shutdown_flushes_pending_batch` (fuerza shutdown, verifica flush).

**Gate**: `pytest cmd/inference-worker/tests -v` (50+ tests) + `pkg/tests` + smoke E2E con LLM real

**Commit**: `refactor(inference): migrate to BaseWorker, batch via _on_message_processed hook + cleanup`

### 1.5 — completion-worker → `BasePubSubWorker` (+ `_parse_pubsub_message`)

**Contrato**: `class CompletionWorker(BasePubSubWorker)` → `handle_event(event: Dict) -> None`

**NOTA**: No es RabbitMQ. Es Redis pub/sub. `BasePubSubWorker` usa
`_parse_pubsub_message(message)` antes de llamar `handle_event(event)`.
`event` llega ya parseado (json.loads hecho por la base).

**Eliminar**:
- Loop pubsub manual con `pubsub.listen()` (bloqueante, sin shutdown graceful)
- `pubsub.listen()` → reemplazado por `get_message(timeout=1.0)` de la base
- Exponential backoff de reconexión (heredado de la base)
- `start_http_server`, health
- `basicConfig` propio

**Preservar**:
- `finalize_job`, `check_job_completion`
- `deduplicate_entities` (fuzzy matching con rapidfuzz)
- `send_webhook` (HMAC-SHA256)
- `_check_and_notify_batch`, `_send_batch_webhook`
- `save_results_to_file`
- `_generate_inference_embeddings` → reemplazar por import del helper compartido

**Double Redis client**: `BasePubSubWorker` ya expone `self.redis_raw` (decode_responses=False).
Usarlo para `msgpack.unpackb()`. Eliminar la creación manual del segundo cliente.

**handle_event simplificado** (ya no necesita filtrar type + json.loads):
```python
def handle_event(self, event: Dict) -> None:
    event_type = event.get("event_type")
    job_id = event.get("job_id")
    if event_type == "job_progress" and job_id:
        self.check_job_completion(job_id)
```

**Conftest**: Variant A. Mocks de Redis pubsub.

**Gate**: `pytest cmd/completion-worker/tests -v` + `pkg/tests` + smoke E2E job completo

**Commit**: `refactor(completion): migrate to BasePubSubWorker, use _parse_pubsub_message + redis_raw`

### 1.6 — audio-worker → `BaseAsyncWorker`

**Contrato**: `class AudioWorker(BaseAsyncWorker)` → `async def process_message(message: Dict) -> None`

**Eliminar**:
- `connect()` manual (aio_pika `connect_robust`, declare_queue, queue.consume — todo heredado)
- `register_signal_handlers` manual (la base usa `loop.add_signal_handler`)
- `start_http_server`, health
- `basicConfig` redundante (llamaba `basicConfig` + `setup_logging`)

**Preservar**:
- `WhisperClientPool` (llamadas de 300s vía `run_in_executor`)
- `SegmentChunker`

**Gate**: `pytest cmd/audio-worker/tests -v` + smoke E2E audio real

**Commit**: `refactor(audio): migrate to BaseAsyncWorker`

### 1.7 — image-worker → `BaseAsyncWorker` + autoría de tests

**Contrato**: `class ImageWorker(BaseAsyncWorker)` → `async def process_message(message: Dict) -> None`

**Eliminar** (mismo patrón que audio):
- `connect()` manual
- `register_signal_handlers` manual
- `start_http_server`, health
- `basicConfig` redundante

**Preservar**:
- `MultimodalLLMClientPool`
- `chunk_text` — re-importar de `pkg.worker_common.chunking`

**Autoría de tests** (15 tests):

| Test | Categoría |
|------|-----------|
| `test_process_message_with_image` | Happy path |
| `test_process_message_connection_error_triggers_retry` | Error transitorio |
| `test_process_message_generic_exception_dlq` | Error permanente |
| `test_chunking_integration` | Chunking |
| `test_multimodal_client_timeout` | Timeout |
| `test_worker_init_config` | Init |
| `test_signal_handler_graceful_shutdown` | Shutdown |
| `test_corrupt_image_data` | Edge case |
| `test_unsupported_format_bmp_tiff` | Edge case |
| `test_malformed_image_metadata` | Edge case |
| `test_concurrent_large_images` | Stress |
| `test_chunk_boundary_conditions` | Edge case |
| `test_redis_store_results` | Integration |
| `test_empty_image_handled` | Edge case |
| `test_large_image_chunked` | Edge case |

**Conftest**: Variant A (mock aio_pika/redis/prometheus).

**Gate**: nuevos tests pasan + smoke E2E imagen real

**Commit**: `refactor(image): migrate to BaseAsyncWorker + add test suite`

### 1.8 — Cleanup final

- Verificar `grep -rn "queue_declare\|declare_queue" cmd/*/ --include="*.py" | grep -v test | grep -v __pycache__` → solo en `pkg/worker_common/base.py` y `pkg/worker_common/async_base.py`
- Verificar `grep -rn "class.*Worker" cmd/*/ --include="*.py" | grep -v test | grep -v __pycache__` → todos heredan de BaseWorker/BaseAsyncWorker/BasePubSubWorker (excepto extraction que es plain)
- Verificar `grep -rn "worker\.process_message\|worker\.handle_event" cmd/*/ --include="*.py"` → firma correcta según contrato
- Verificar `grep -rn "basicConfig" cmd/*/ --include="*.py" | grep -v test | grep -v __pycache__` → 0 matches (todo heredado de bases)
- Verificar `grep -rn "torch.cuda.empty_cache" cmd/embeddings-worker/ cmd/entities-worker/` → presente en cleanup
- Verificar `grep -rn "_parse_pubsub_message" pkg/worker_common/pubsub_base.py` → presente
- Verificar `grep -rn "def cleanup" pkg/worker_common/base.py pkg/worker_common/async_base.py pkg/worker_common/pubsub_base.py` → presente (no-op en cada base)
- Verificar que `chunking.py` es importado por ≥1 worker
- Eliminar cualquier archivo `worker.py` sobrante tras los renombres
- `make test-python` completo

**Commit**: `chore: verify base class usage, cleanup stale worker.py files`

---

## Fase 2 — Smoke Test E2E Integral

### Plan de Rollback

| Escenario | Acción |
|-----------|--------|
| Fase 1 falla (migración rota) | Tag `pre-migration-snapshot` = punto de retorno. `git reset --hard pre-migration-snapshot` en branch `recovery/bases-compartidas` |
| Fase 2 falla tras merge a main | `git revert <merge-commit>` del merge a main (revert completo) |
| Fase 3 cherry-pick falla | Revert individual del commit del cherry-pick (no afecta otros) |
| Rollback parcial | Prioridad inversa a migración: completion → inference → entities → embeddings → audio → image |

### Smoke tests

```bash
# 1. Tests automatizados
make docker-up  # rebuild all images, wait for 16/16 healthy
make test-python
make test       # Go tests
make lint

# 2. Verificaciones grep (automatizar en script)
grep -rn "basicConfig" cmd/*/ --include="*.py" | grep -v test | grep -v __pycache__
grep -rn "torch.cuda.empty_cache" cmd/embeddings-worker/ entities-worker/
grep -rn "_parse_pubsub_message" pkg/worker_common/pubsub_base.py
grep -rn "def cleanup" pkg/worker_common/base.py

# 3. Pruebas manuales
# Documento: subir PDF → pipeline completo → resultados en <30s
# Audio: subir .wav → whisper → completion
# Imagen: subir .jpg → multimodal → completion
# Verificar logs de cada worker (no DLX errors, no reconnect loops)
# Verificar resultados en Redis: key orchestrator:job:{id}:*
# Verificar archivo de resultados en disco

# 4. Comparativa con baseline
echo "Comparar con baseline en /tmp/baseline-e2e.txt"
echo "Comparar metrics con /tmp/baseline-metrics.json"
```

**Gate**: TODO verde → merge a main (commit merge, no squash).

---

## Fase 3 — Cherry-pick de features

Sobre la base ya consolidada, aplicar features del tag `pre-revert-backup`:

| # | Commit hash | Feature | Esfuerzo |
|---|-------------|---------|----------|
| 3.1 | `064c6b7` | CI updates (actions v3→v4) | 0 conflictos |
| 3.2 | `aca60c0` | API key auth middleware | `config.go` conflicto menor |
| 3.3 | `64dc548` | Race fixes (pubsub timeout, Lua DECR) | Medio — pubsub_base apply clean, Lua DECR port manual |
| 3.4 | `47fb7b2` | Adaptive Flow Control | Alto — `adaptive_semaphore.py`+`admission.go`+`errors.go` nuevos, `worker.py`/`config.go` merge |
| 3.5 | `47b0491` | Ingestion API endpoints | Solo `results.go`+`entity_utils.py`+routes; omitir deletions |
| 3.6 | `5885f94` | Cache key improvement | Port manual CACHE_VERSION a _cache_key |
| 3.7 | `90cdc29` | Ingestion API spec docs | Trivial (docs/swagger) |

Cada cherry-pick en commit separado con `make test-python && make test` verde tras cada uno.

---

## Contratos Base (referencia)

### BaseWorker (sync, pika)

```python
class BaseWorker:
    def __init__(self, worker_name, queue_name, metrics_port, requires_gpu=False)
    def process_message(self, message: Dict) -> Any     # ← subclase implementa
    def run(self)                                        # entry point (loop pika)
    def _on_message_processed(self)                      # hook, llamado tras cada msg
    def _publish_to_queue(self, queue, message)          # helper publicación
    def cleanup(self)                                    # hook shutdown (no-op default)
    # Hooks: _get_retry_count, _should_retry, _handle_transient_error
    # Hereda: Redis (auto-reconnect), EventBus, ResourceManager, Prometheus, health, signals
```

### BaseAsyncWorker (async, aio_pika)

```python
class BaseAsyncWorker:
    def __init__(self, worker_name, queue_name, metrics_port, requires_gpu=False)
    async def process_message(self, message: Dict) -> None  # ← subclase implementa
    async def run(self)                                      # entry point (loop aio_pika)
    async def run_in_executor(self, func, *args)             # blocking calls
    def cleanup(self)                                        # hook shutdown (no-op default)
    # Hereda: Redis (lazy), EventBus, Prometheus, health, signals (loop.add_signal_handler)
```

### BasePubSubWorker (sync, Redis pub/sub)

```python
class BasePubSubWorker:
    def __init__(self, worker_name, metrics_port)            # SIN queue_name
    def handle_event(self, event: Dict) -> None              # ← subclase implementa
    def start(self)                                          # entry point (loop pubsub)
    def cleanup(self)                                        # hook shutdown (no-op default)
    def _parse_pubsub_message(self, msg: Dict) -> Optional[Dict]  # parsea + filtra
    # Propiedades: redis_client, redis_raw (decode=False), event_bus
    # Hereda: Redis (2 clientes), Prometheus, health, signals
```

---

## Riesgos y mitigaciones

| Riesgo | Mitigación |
|--------|-----------|
| Contrato async roto (process_message vs _process_message_async) | Usar firma exacta: `async def process_message(self, message: Dict) -> None` |
| DLX mismatch reintroducido | Bases ya tienen DLX args post-d231138. Workers NO declaran colas. Verificar con grep. |
| Shutdown thread timer ghost en inference | cleanup() join(timeout=2.0) + flush final + test de shutdown |
| GLiNER offline roto tras rename | Test `docker run --network=none entities-worker` tras migración |
| inference-worker: 50 tests → alguno roto por batch hook | Preservar todos los mocks; añadir test `test_timer_shutdown_flushes_pending_batch` |
| completion-worker: pubsub handle_event recibe evento parseado | _parse_pubsub_message() en base filtra + parsea; worker recibe dict ya parseado |
| image-worker 0 tests → migración ciega | Autoría de 15 tests antes de migrar (incluyendo edge cases) |
| base.py version diff HEAD vs e5e63c9 (73 líneas extra) | NO revertir base.py — mantener HEAD que tiene DLX fix + más features |
| GPU memory leak en shutdown | cleanup() con `del model` + `torch.cuda.empty_cache()` en embeddings y entities |
| Logging inconsistente tras migración | async_base y pubsub_base migrados a setup_logging(); workers heredan sin basicConfig propio |
| Redis connection pool sin límite explicito | No urgente: cada worker es 1 proceso, 1-2 clientes. Tech debt posterior. |

---

## Criterios de aceptación (pre-merge)

1. `make test-python` — 100% verde
2. `make test` — 100% verde (Go)
3. `make lint` — sin errores
4. `docker ps` — 16/16 healthy tras rebuild
5. E2E documento: subir → pipeline completo → resultados en <30s
6. E2E audio: subir .wav → whisper → completion → resultados
7. E2E imagen: subir .jpg → multimodal → completion → resultados
8. `grep -rn "queue_declare\|declare_queue" cmd/*/ --include="*.py" | grep -v test | grep -v __pycache__` → solo en `pkg/worker_common/base.py` y `pkg/worker_common/async_base.py`
9. `grep -rn "class.*Worker" cmd/*/ --include="*.py" | grep -v test | grep -v __pycache__` → todos heredan de BaseWorker/BaseAsyncWorker/BasePubSubWorker (excepto extraction que es plain)
10. Tras cleanup: todos los `pkg/worker_common/*.py` tienen ≥1 importador en `cmd/*/`
11. `grep -rn "basicConfig" cmd/*/ --include="*.py" | grep -v test | grep -v __pycache__` → 0 matches (logging heredado de bases)
12. `grep -rn "torch.cuda.empty_cache" cmd/embeddings-worker/ cmd/entities-worker/` → presente en cleanup
13. `grep -rn "_parse_pubsub_message" pkg/worker_common/pubsub_base.py` → presente y usado por BasePubSubWorker.start()
14. Tiempo E2E no empeora vs baseline (margen: +10% max)
