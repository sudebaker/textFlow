# Plan de Optimización de Rendimiento — textFlow

> **Objetivo:** Reducir latencia end-to-end del pipeline, eliminar cuellos de botella críticos y aumentar throughput sin degradar calidad de resultados.
>
> **Enfoque:** Optimizaciones incrementales con arquitectura estable. Sin reescrituras masivas. Prioridad por impacto/riesgo.

---

## Fase 1: Crítica — Jobs que nunca finalizan (Completion Worker)

**Problema:** El completion worker depende de Redis pub/sub (`job:events`) para detectar cuando un job está listo. Si se pierde un evento (worker reiniciado, desconexión de red), el job permanece en estado `processing` para siempre.

**Arquitectura actual:**
```
Workers → Redis pub/sub "job:events" → Completion Worker (pubsub.listen())
```

**Plan:** Añadir un **reconciliation loop** que escanee periódicamente jobs activos cuyos `steps` indican completitud pero cuyo estado sigue siendo `processing`.

### Tasks

**Task 1: Implementar `ReconciliationService` en `cmd/completion-worker/`**

- Crear `reconciliation_service.py` con un thread que cada 30 segundos:
  - `SCAN` o `ZRANGE` en `active_jobs` para jobs con `status=processing`
  - Para cada job, `HGETALL` en `:steps` y verificar si todos los steps requeridos están `completed`
  - Si está completo pero status != completed, invocar `finalize_job()` forzosamente
- Thread con `daemon=True`, arranca en `__init__` del worker

**Task 2: Añadir métrica de "stale jobs rescued"**

- Loguear + contador de cuántos jobs fueron rescatados por reconciliación

**Task 3: Test de reconocimiento de job huérfano**

- Simular job con steps completos pero status `processing`
- Verificar que reconciliación lo detecta y finaliza

---

## Fase 2: Alta — Embeddings de inferencias por batch global

**Problema:** Tanto `embeddings-worker` como `completion-worker` generan embeddings de inferencias **chunk por chunk**. Si un job tiene 100 chunks con 10 inferencias cada uno, hacen 100 llamadas a `model.encode()` en lugar de 1 llamada con 1000 textos.

**Plan:** Aplanar todos los textos de inferencias de todos los chunks en una sola lista, generar embeddings en un solo batch, luego remapear a chunks.

### Tasks

**Task 4: Batch global en `embeddings-worker` (`_generate_inference_embeddings`)**

Archivos:
- Modificar: `cmd/embeddings-worker/worker.py`

Actualizar `_generate_inference_embeddings` para:
- Recolectar TODOS los `(chunk_id, inference_text)` en una lista plana
- Llamar `model.encode(all_texts, batch_size=EMBEDDING_BATCH_SIZE)` una sola vez
- Remapear resultados de vuelta a `Dict[chunk_id, Dict[idx, embedding]]`
- Tiempo estimado: de O(N_chunks) a O(1) en llamadas a encode

**Task 5: Batch global en `completion-worker` (`_generate_inference_embeddings`)**

Archivos:
- Modificar: `cmd/completion-worker/worker.py`

- Misma refactorización que en embeddings-worker
- Eliminar la lógica de fallback que carga SentenceTransformer (ya no necesaria si embeddings-worker siempre genera)

**Task 6: Tests de batch global**

- Verificar que embeddings de inferencias con múltiples chunks producen resultados correctos
- Verificar que el orden y mapeo chunk→inference se preserva

---

## Fase 3: Alta — Conexión RabbitMQ reutilizada en Entities Worker

**Problema:** `entities-worker` abre una **nueva conexión `pika.BlockingConnection`** por cada job que publica inferencias. Esto implica TCP handshake + TLS + AMQP handshake + autenticación por job.

**Plan:** Reutilizar el canal del consumidor para publicar mensajes downstream.

### Tasks

**Task 7: Refactorizar `entities-worker` para publisher reutilizado**

Archivos:
- Modificar: `cmd/entities-worker/worker.py`

- En `worker.__init__`, crear un segundo canal sobre la misma conexión: `self._publisher_channel`
- En `process()`, usar `self._publisher_channel.basic_publish()` en lugar de `pika.BlockingConnection()`
- Añadir `try/except` para reconexión lazy si el canal se cierra

**Task 8: Test de conexión reutilizada**

- Verificar que tras procesar N jobs, solo existe 1 conexión activa
- Verificar que publicación sigue funcionando tras error transitorio

---

## Fase 4: Media-Alta — Pipeline Redis en Orchestrator

**Problema:** `getJobHandler` y `CreateBatchHandler` hacen múltiples lecturas/escrituras Redis secuenciales en lugar de pipelines.

**Plan:** Implementar métodos de pipeline en `internal/redis/client.go` y usarlos en handlers.

### Tasks

**Task 9: Añadir `GetJobStatusPipelined()` en `internal/redis/client.go`**

- Un solo `Pipeline()` que hace `HGet(status)` + `HGetAll(steps)` + `Get(error)` + `HGet(created_at)` + `HGet(webhook)`
- Retornar estructura `JobStatusSnapshot` consolidada

**Task 10: Refactorizar `getJobHandler` en `cmd/orchestrator/main.go`**

- Reemplazar 4 llamadas Redis secuenciales por una sola llamada a `GetJobStatusPipelined()`

**Task 11: Añadir `CreateBatchPipelined()` en `internal/redis/client.go`**

- Para batch creation: `Pipeline()` con `HSet(status)` + `HSet(created)` + `HSet(features)` + `HSet(batch_id)` + `SAdd(batch_jobs)` para cada job
- Reducir de ~5 RTT/job a 1 RTT/job

**Task 12: Refactorizar `CreateBatchHandler` en `cmd/orchestrator/handlers/batch.go`**

- Usar `CreateBatchPipelined()` dentro de cada goroutine

**Task 13: Tests de pipeline**

- Test que verifica que `GetJobStatusPipelined` retorna los mismos datos que las 4 llamadas individuales
- Benchmark opcional: medir RTT reducido

---

## Fase 5: Media — Deduplicación de entidades O(N²) → O(N)

**Problema:** `deduplicate_entities()` en `entities-worker` y `completion-worker` es O(N²) con `fuzz.ratio`. Para miles de entidades, esto es lento.

**Plan:** Usar un diccionario indexado por `(normalized_text + label)` como pre-filtro antes del fuzzy matching.

### Tasks

**Task 14: Optimizar `deduplicate_entities()` en `cmd/entities-worker/worker.py`**

Archivos:
- Modificar: `cmd/entities-worker/worker.py`

- Normalizar texto (lowercase, strip spaces)
- Crear dict `keyed = {(norm_text, label): entity}`
- Solo aplicar `fuzz.ratio` si hay colisiones de clave (mismo texto normalizado + label)
- Convierte O(N²) en O(N) para la mayoría de casos

**Task 15: Optimizar `deduplicate_entities()` en `cmd/completion-worker/worker.py`**

Archivos:
- Modificar: `cmd/completion-worker/worker.py`

- Misma refactorización que en entities-worker
- Evaluar si se puede eliminar completamente si entities-worker ya deduplica con threshold suficiente

**Task 16: Tests de deduplicación**

- Verificar que 1000 entidades con 500 duplicadas se procesan en <1s
- Verificar que resultado es idéntico al algoritmo O(N²) original

---

## Fase 6: Media — Worker Extraction: Prefetch y Regex

**Problema:**
- `EXTRACTION_CONCURRENCY=5` puede subutilizar el worker si Docling es rápido
- Regex patterns en `SourceClassifier` se compilan en cada llamada
- Regex en entities worker (`_extract_dates`, etc.) también se compilan en cada llamada

**Plan:** Aumentar prefetch y compilar regexes una sola vez.

### Tasks

**Task 17: Compilar regexes en `SourceClassifier`**

Archivos:
- Modificar: `cmd/extraction-worker/`

- Mover `PATTERNS` de strings a `re.compile()` a nivel de clase
- Actualizar `_classify_by_patterns()` para usar patterns compilados

**Task 18: Compilar regexes en entities worker**

Archivos:
- Modificar: `cmd/entities-worker/worker.py`

- Crear módulo-level compiled patterns para `_extract_dates`, `_extract_money`, `_extract_orgs`, `_extract_locs`, `_extract_persons`
- Usar `compiled_pattern.search()` en lugar de `re.search(pattern_string, ...)`

**Task 19: Hacer `EXTRACTION_CONCURRENCY` configurable vía env var**

- Default subir a 10 (desde 5)
- Añadir validación: `max(1, min(50, value))`

**Task 20: Tests**

- Verificar que SourceClassifier produce mismo resultado con patterns compilados
- Verificar que entities regex extractors producen mismos resultados

---

## Fase 7: Media-Baja — Infraestructura Docker y GPU

**Problema:** Múltiples workers GPU (`embeddings`, `entities`, `completion`, `docling`) comparten `CUDA_VISIBLE_DEVICES=0`, generando contención.

**Plan:** Estrategias de mitigación sin requerir hardware adicional.

### Tasks

**Task 21: Docker Compose: MPS (Multi-Process Service) para GPU**

Archivos:
- Modificar: `docker-compose.gpu.yml`

- Añadir `runtime: nvidia` con `capabilities: [gpu]` donde corresponda
- Añadir variable `NVIDIA_MPS_ACTIVE=1` para permitir sharing de contexto CUDA
- Documentar el cambio

**Task 22: Reducir `PREFETCH_COUNT` de embeddings worker**

- De 20 a 8 para evitar acumulación de mensajes que sature VRAM
- Añadir comentario explicativo en docker-compose

**Task 23: Añadir `torch.cuda.empty_cache()` en workers GPU**

- En `embeddings-worker`, después de cada job grande (>1000 chunks)
- En `entities-worker`, después de cada batch GLiNER
- En `completion-worker`, después de generar inference embeddings
- Controlable vía env var `CUDA_EMPTY_CACHE=true|false`

**Task 24: Cambiar Redis `maxmemory-policy`**

- En `docker-compose.yml`: cambiar de `noeviction` a `allkeys-lru`
- Esto previene rechazo de escrituras cuando Redis alcanza el límite de 1 GB

---

## Fase 8: Baja — RabbitMQ Pool y Rate Limiting

**Problema:** `RabbitMQPoolSize=5` puede ser insuficiente bajo alta carga de publicaciones concurrentes.

**Plan:** Escalar pool y añadir throttling interno.

### Tasks

**Task 25: Aumentar `RabbitMQPoolSize` a 20**

Archivos:
- Modificar: `internal/config/config.go`

- Cambiar default de 5 a 20
- Justificar con comentario: batch mode puede publicar N jobs simultáneamente

**Task 26: Añadir `Semaphore` en workers Python para Docling/vLLM**

- En `extraction-worker`: limitar a `MAX_DOCLING_CONCURRENT` (default 5)
- En `inference-worker`: limitar a `MAX_VLLM_CONCURRENT` (default 10)
- Evita saturar servicios externos bajo burst de jobs

**Task 27: Tests de semáforo**

- Simular 20 jobs concurrentes
- Verificar que solo N acceden a Docling/vLLM simultáneamente

---

## Métricas de Validación

Antes y después de cada fase, medir:

| Métrica | Cómo medir | Target |
|---------|-----------|--------|
| Latencia job simple (PDF 10 páginas) | `time client -i doc.pdf -o out.json` | -30% |
| Latencia job con inferencias | `time client -i doc.pdf -o out.json -f` | -40% |
| Throughput batch (100 docs) | `time client -b batch.json -o out.json` | -25% |
| Jobs colgados (processing >1h) | `redis-cli ZRANGE active_jobs -inf +inf` | 0 |
| Redis RTT por poll | Log interno o `redis-cli --latency` | -60% |
| GPU VRAM usage | `nvidia-smi` durante carga | Sin OOM |

---

## Orden de Implementación Recomendado

| Orden | Fase | Riesgo | Tiempo estimado | Impacto |
|-------|------|--------|-----------------|---------|
| 1 | Fase 1 (Reconciliación) | Bajo | 2-3h | Crítico |
| 2 | Fase 3 (RabbitMQ reuse) | Bajo | 1-2h | Alto |
| 3 | Fase 2 (Batch global embeddings) | Medio | 3-4h | Alto |
| 4 | Fase 4 (Redis pipeline) | Medio | 3-4h | Medio-Alto |
| 5 | Fase 5 (Deduplicación O(N)) | Bajo | 2h | Medio |
| 6 | Fase 6 (Regex + prefetch) | Bajo | 1-2h | Medio |
| 7 | Fase 7 (Infra MPS + cache) | Medio | 2-3h | Medio |
| 8 | Fase 8 (Pool + semáforos) | Bajo | 1-2h | Bajo |

**Tiempo total estimado:** ~15-20 horas de desarrollo + testing.
