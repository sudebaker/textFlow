# Performance Optimization Plan — IA Text Orchestrator

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reducir latencia end-to-end y aumentar throughput del pipeline de procesamiento de documentos, abordando cuellos de botella identificados en infraestructura, ML inference y flujo de datos.

**Baseline actual:** Pipeline típico (PDF 10 páginas, ~100 chunks):
- Docling: ~25s (variable: 10-60s)
- Embeddings GPU: ~0.2s
- Entities CPU: ~15-30s (⚠️ sin batching real)
- **Total: ~40-90s** (scenario óptimo GPU)

**Meta:** Reducir un 30-50% la latencia total en escenarios típicos.

---

## Phase 1 — Quick Wins (Alto Impacto, Bajo Esfuerzo)

Estos cambios requieren <1 día de trabajo y tienen impacto inmediato.

---

### Optimization #1: Habilitar GPU para Entities Worker

**Diagnóstico:** `ENTITIES_DEVICE=cpu` en `.env.example` (línea 158). El worker GLiNER corre en CPU por default aunque haya GPU disponible, haciendo NER **10-50x más lento**.

**Solución:** Cambiar el default y añadir auto-detección como embeddings-worker.

**Files:**
- Modify: `cmd/entities-worker/worker.py`
- Modify: `cmd/entities-worker/main.py`
- Modify: `.env.example`

**Steps:**

- [x] **Step 1: Leer `cmd/entities-worker/worker.py`** — localizar línea ~88 con `ENTITIES_DEVICE`

- [x] **Step 2: Añadir auto-detección GPU** (mismo patrón que embeddings-worker):

```python
# En worker.py, cerca de la línea 88
def _setup_device(self):
    device_param = os.getenv("ENTITIES_DEVICE", "").lower()
    if not device_param:
        device_param = "cuda" if torch.cuda.is_available() else "cpu"
    self.device = torch.device(device_param)
    logger.info(f"Using device: {self.device}")
```

- [x] **Step 3: Mover modelo a GPU post-carga** en `main.py` línea ~221:

```python
# AFTER
self.model.to(self.device)

# Verify GPU utilization
if self.device.type == "cuda":
    logger.info(f"GLiNER GPU memory: {torch.cuda.memory_allocated()/1e9:.2f}GB")
```

- [x] **Step 4: Actualizar `.env.example`** — cambiar default a `ENTITIES_DEVICE=cuda`

- [x] **Step 5: Test local**

```bash
cd cmd/entities-worker
python -c "
import torch
from worker import EntitiesWorker
# Solo verificar que device detection works
print(f'CUDA available: {torch.cuda.is_available()}')
"
```

**Impacto estimado:** 🔴 **Crítico** — Reduce NER de ~15-30s a **~0.5-2s** en GPU (10-50x speedup)

**Esfuerzo:** 🟢 **Bajo** — ~1 hora

**Métricas de éxito:**
- `model_inference_duration_seconds{device="cuda"}` vs `device="cpu"`
- Tiempo total de entities worker por job

---

### Optimization #2: Batching Real para Entities Worker

**Diagnóstico:** El worker procesa **1 chunk a la vez** (línea ~592 en worker.py), pero GLiNER soporta batch_size=32. overhead de Python loop + model call por chunk es enorme.

**Solución:** Acumular chunks en batch y procesarlos juntos.

**Files:**
- Modify: `cmd/entities-worker/worker.py`

**Steps:**

- [x] **Step 1: Leer la función `_handle_message`** para entender el procesamiento actual

- [x] **Step 2: Implementar batching con cola acumuladora**:

```python
class EntitiesWorker(BaseWorker):
    def __init__(self, ...):
        super().__init__(...)
        self._batch = []  # chunks pendientes
        self._batch_max_size = int(os.getenv("GLINER_BATCH_SIZE", "32"))
        self._batch_timeout = float(os.getenv("GLINER_BATCH_TIMEOUT", "0.5"))  # segundos
        self._batch_last_processed = time.time()

    def _handle_message(self, ch, method, properties, body):
        job_data = json.loads(body)
        self._batch.append((ch, method, properties, job_data))
        
        # Procesar si batch lleno o timeout
        if len(self._batch) >= self._batch_max_size or \
           time.time() - self._batch_last_processed > self._batch_timeout:
            self._process_batch()

    def _process_batch(self):
        if not self._batch:
            return
        
        batch_data = [item[3] for item in self._batch]
        # Procesar todos juntos
        results = self._model.predict(batch_data, ...)  # GLiNER batch predict
        
        for i, (ch, method, properties, job_data) in enumerate(self._batch):
            # Guardar result individual y ack
            ...
        
        self._batch = []
        self._batch_last_processed = time.time()
```

- [x] **Step 3: Añadir `GLINER_BATCH_TIMEOUT` a `.env.example`**

- [x] **Step 4: Test de throughput**

```bash
# Comparar antes/después
python -c "
import time
# Simular 100 chunks
"
```

**Impacto estimado:** 🟡 **Medio-Alto** — 5-10x speedup en NER (procesa 32 chunks en ~1s vs 32s secuencial)

**Esfuerzo:** 🟡 **Medio** — ~3-4 horas (hay que evitar race conditions con RabbitMQ ack)

---

### Optimization #3: Reducir Overhead de JSON en Embeddings

**Diagnóstico:** Embeddings float32 se almacenan como JSON (~400KB por documento). MessagePack reduciría a ~100KB.

**Solución:** Usar msgpack para serialización binaria en Redis.

**Files:**
- Modify: `cmd/embeddings-worker/worker.py` (guardar)
- Modify: `internal/redis/client.go` (cargar)
- Modify: `pkg/requirements.txt` (añadir msgpack)

**Steps:**

- [x] **Step 1: Añadir msgpack a requirements del worker**

```bash
# En cmd/embeddings-worker/requirements.txt
msgpack>=1.0.0
```

- [x] **Step 2: Modificar guardar embeddings** en worker.py (~línea 180):

```python
import msgpack

# Guardar como msgpack (binario)
embeddings_packed = msgpack.packb(embeddings_list, use_bin_type=True)
self.redis_client.set(f"orchestrator:job:{job_id}:embeddings", embeddings_packed)
```

- [x] **Step 3: Modificar lectura en Redis client.go** (~línea 333):

```go
import "github.com/vmihailenco/msgpack/v5"

var embeddings []float32
if err := msgpack.Unmarshal(data, &embeddings); err != nil {
    return nil, err
}
```

- [x] **Step 4: Añadir `go.mod` replace si es necesario**

```bash
cd internal/redis && go get github.com/vmihailenco/msgpack/v5
```

- [x] **Step 5: Test de serialización**

```bash
# Python
python -c "
import msgpack, json, sys
data = [1.0] * 1024
print(f'JSON: {len(json.dumps(data))} bytes')
print(f'MsgPack: {len(msgpack.packb(data, use_bin_type=True))} bytes')
"
```

**Impacto estimado:** 🟡 **Medio** — ~75% reducción en tamaño embeddings, menos Redis memory + network

**Esfuerzo:** 🟡 **Medio** — ~4 horas (cambios en ambos lados del pipeline)

---

### Optimization #4: Aumentar Prefetch de Workers

**Diagnóstico:** prefetch de extraction=3, embeddings/entities=5 es muy conservador para workers que procesan rápido.

**Solución:** Aumentar prefetch para aprovechar mejor la capacidad de procesamiento.

**Files:**
- Modify: `deploy/docker/docker-compose.yml`

**Steps:**

- [x] **Step 1: Leer docker-compose.yml** — localizar prefetch de cada worker

- [x] **Step 2: Aumentar prefetch counts**:

```yaml
# extraction-worker: 3 → 10
extra_configs:
  - rabbitmq.prefetch: 10

# embeddings-worker: 5 → 20 (GPU puede manejar más)
# entities-worker: 5 → 15 (GPU puede manejar más)
```

- [x] **Step 3: Ajustar memoria del metadata-worker** (512MB es muy bajo para prefetch=10)

```yaml
metadata-worker:
  deploy:
    resources:
      limits:
        memory: 1G  # ↑ de 512M
```

- [x] **Step 4: Verificar docker-compose config**

```bash
docker compose -f deploy/docker/docker-compose.yml config --quiet
```

**Impacto estimado:** 🟡 **Medio** — Mejora throughput bajo carga al mantener workers ocupados

**Esfuerzo:** 🟢 **Bajo** — 30 minutos

---

## Phase 2 — Optimizaciones de Media Complejidad

---

### Optimization #5: Eliminar SCAN Periódico en Redis

**Diagnóstico:** `ExpireStuckJobs` usa `SCAN` con patrón `job:*:meta` cada 60s. En producción con miles de jobs, esto causa latencia Redis spikes.

**Solución:** Usar Sorted Set con timestamps como score para tracking de jobs activos.

**Files:**
- Modify: `internal/redis/client.go`
- Modify: `cmd/orchestrator/main.go` (actualizar job creation/deletion)
- Modify: `internal/redis/client_test.go`

**Steps:**

- [x] **Step 1: Leer `ExpireStuckJobs`** en client.go (~línea 544)

- [x] **Step 2: Añadir Sorted Set para tracking**:

```go
const activeJobsKey = "orchestrator:active_jobs"  // ZSET

// En SetJobCreated:
zadd := redis.Z{Score: float64(time.Now().Unix()), Member: job.ID}
c.client.ZAdd(ctx, activeJobsKey, zadd)

// En DeleteJob:
c.client.ZRem(ctx, activeJobsKey, jobID)

// En ExpireStuckJobs (reemplazar SCAN):
// Obtener jobs con score < (now - stuck_threshold)
cutoff := time.Now().Unix() - int64(stuckThreshold.Seconds())
oldJobs, err := c.client.ZRangeByScore(ctx, activeJobsKey, &redis.ZRangeByScore{
    Min: "-inf",
    Max: fmt.Sprintf("%d", cutoff),
}).Result()
```

- [x] **Step 3: Tests** — verificar que el nuevo método encuentra los mismos jobs stuck

**Impacto estimado:** 🟡 **Medio** — Elimina SCAN O(n) completo, reduce latency p99 de Redis

**Esfuerzo:** 🟡 **Medio** — ~6 horas

---

### Optimization #6: Redis Pool Config Explícita

**Diagnóstico:** go-redis usa PoolSize default = runtime.NumCPU (~10-16). Con 100+ goroutines puede haber contención.

**Solución:** Configurar PoolSize explícitamente en el orchestrator.

**Files:**
- Modify: `internal/redis/client.go` (~línea 64)

**Steps:**

- [x] **Step 1: Leer cómo se crea el cliente Redis**

- [x] **Step 2: Añadir PoolSize explícito**:

```go
func NewRedisClient(addr, password string, jobTTL time.Duration) (*RedisClient, error) {
    opts := &redis.Options{
        Addr:        addr,
        Password:    password,
        DB:          0,
        PoolSize:    100,           // ↑ de default NumCPU
        MinIdleConns: 10,           // mantener conexiones ready
        PoolTimeout:  4 * time.Second,
        DialTimeout:  5 * time.Second,
        ReadTimeout:  3 * time.Second,
        WriteTimeout: 3 * time.Second,
    }
    // ...
}
```

- [x] **Step 3: Test de carga** — verificar que no hay `connection pool exhausted` errores

**Impacto estimado:** 🟡 **Medio** — Previene connection starvation bajo load alta

**Esfuerzo:** 🟢 **Bajo** — 1 hora

---

### Optimization #7: GOGC y GOMEMLIMIT en Orchestrator

**Diagnóstico:** Sin configuración, Go GC corre frecuentemente con heap grande, causando pauses.

**Solución:** Configurar GOGC y opcionalmente GOMEMLIMIT.

**Files:**
- Modify: `cmd/orchestrator/Dockerfile`

**Steps:**

- [x] **Step 1: Añadir environment variables al Dockerfile**:

```dockerfile
ENV GOGC=150
ENV GOMEMLIMIT=800MiB
```

- [x] **Step 2: Test de stress** — ejecutar carga y verificar GC pauses con `GODEBUG=gctrace=1`

**Impacto estimado:** 🟢 **Bajo** — Reduce GC overhead ~20-30% en workloads con mucha asignación

**Esfuerzo:** 🟢 **Bajo** — 30 minutos

---

### Optimization #8: torch.compile para BGE-M3

**Diagnóstico:** Modelos cargan en eager mode. `torch.compile()` puede dar 10-30% speedup en inference.

**Solución:** Aplicar `torch.compile()` post-carga del modelo.

**Files:**
- Modify: `cmd/embeddings-worker/embeddings.py`

**Steps:**

- [x] **Step 1: Leer cómo se carga el modelo BGE-M3** (~línea 100)

- [x] **Step 2: Añadir torch.compile con dynamo**:

```python
# AFTER: model = AutoModel.from_pretrained(...)
if os.getenv("TORCH_COMPILE", "false").lower() == "true":
    logger.info("Compiling model with torch.compile (first inference will be slower)...")
    self.model = torch.compile(self.model, mode="reduce-overhead")
```

- [x] **Step 3: Añadir `TORCH_COMPILE=true` a `.env.example`** (opcional, default false)

**Impacto estimado:** 🟢 **Bajo-Medio** — 10-30% speedup en inference, pero primera inferencia 5-10x más lenta (compilation time)

**Esfuerzo:** 🟢 **Bajo** — 1 hora

---

## Phase 3 — Optimizaciones Avanzadas

---

### Optimization #9: Async Polling para Docling

**Diagnóstico:** extraction-worker hace blocking `requests.get()` con long-polling de 30s. Si Docling responde en 5s, el worker está blocked + espera 5s extra mínimo.

**Solución:** Usar asyncio con aiohttp para no bloquear el consumer thread.

**Files:**
- Modify: `cmd/extraction-worker/worker.py`

**Steps:**

- [x] **Step 1: Implementar async polling** con `aiohttp` + `asyncio`:

```python
async def _poll_docling_async(self, task_id: str, timeout: int) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{DOCLING_URL}/v1/status/poll/{task_id}",
                params={"wait": min(30, deadline - time.time())}
            ) as resp:
                data = await resp.json()
                if data["status"] == "success":
                    return data
                await asyncio.sleep(2)  # pequeño delay entre polls
    
    raise TimeoutError(f"Docling polling timeout after {timeout}s")
```

- [x] **Step 2: Integrar en extraction-worker main loop** — reescrito completamente con aio_pika (async nativo), hasta EXTRACTION_CONCURRENCY jobs concurrentes

**Impacto estimado:** 🟡 **Medio** — Reduce latency cuando Docling responde rápido (<30s), no bloquea worker

**Esfuerzo:** 🔴 **Alto** — ~8 horas (requiere refactor significant)

---

### Optimization #10: RabbitMQ Publisher Confirms

**Diagnóstico:** El broker Go usa publish síncrono con mutex pero sin publisher confirms. No hay garantía de entrega a nivel de broker.

**Solución:** Implementar confirms para mayor reliability (y eventualmente batching async).

**Files:**
- Modify: `internal/broker/rabbitmq.go`

**Impacto estimado:** 🟢 **Bajo** — Mejora reliability, no tanto throughput

**Esfuerzo:** 🟡 **Medio** — ~6 horas

---

### Optimization #11: Delayed Retry Queue para Workers

**Diagnóstico:** `retry_with_backoff` en base.py bloquea el consumer thread hasta 60s por retry.

**Solución:** Usar RabbitMQ delayed message exchange para requeue sin bloquear.

**Files:**
- Modify: `pkg/worker_common/base.py`
- Modify: `internal/broker/rabbitmq.go` (config DLX con delay)

**Implementado:**
- [x] `pkg/worker_common/base.py` — delayed retry con x-retry-count header, fallback a time.sleep si plugin ausente
- [x] `internal/broker/rabbitmq.go` — declareDelayedExchange con manejo graceful de 406
- [x] `pkg/tests/test_dlq_pattern.py` — 22 tests de cobertura
- [x] `pkg/worker_common/rabbitmq_async.py` — helpers async para aio_pika

**Impacto estimado:** 🟡 **Medio** — Throughput mejora porque worker no está bloqueado durante retries

**Esfuerzo:** 🟡 **Medio** — ~6 horas

---

## Phase 4 — Optimizaciones de Arquitectura

---

### Optimization #12: Connection Pooling para RabbitMQ Go Broker

**Diagnóstico:** Un solo canal con mutex para publishing. Si el canal se cierra, hay lock contention.

**Solución:** Channel pool o eliminar mutex con connection-per-goroutine.

**Files:**
- Modify: `internal/broker/rabbitmq.go`

**Impacto estimado:** 🟡 **Medio** — Throughput de publishing mejora ~2-3x bajo carga

**Esfuerzo:** 🔴 **Alto** — ~8 horas (cambios significativos en broker)

---

### Optimization #13: Adaptive Chunking Basado en Documento

**Diagnóstico:** Chunk size fijo de 512 tokens puede ser subóptimo para documentos mixtos (tablas vs texto).

**Solución:** Análisis pre-chunking para detectar tablas vs texto y aplicar chunking diferenciado.

**Files:**
- Modify: `cmd/extraction-worker/worker.py`

**Impacto estimado:** 🟢 **Bajo** — Mejora calidad de chunks más que throughput

**Esfuerzo:** 🔴 **Alto** — Requiere análisis de documento (fuera scope inicial)

---

## Resumen de Prioridades

| # | Optimización | Impacto | Esfuerzo | Prioridad |
|---|--------------|---------|----------|-----------|
| 1 | **GPU para entities** | 🔴 Crítico | 🟢 Bajo | **P0** |
| 2 | **Batching entities** | 🟡 Medio-Alto | 🟡 Medio | **P1** |
| 3 | **MsgPack embeddings** | 🟡 Medio | 🟡 Medio | **P1** |
| 4 | **Prefetch workers** | 🟡 Medio | 🟢 Bajo | **P1** |
| 5 | **Eliminar Redis SCAN** | 🟡 Medio | 🟡 Medio | **P2** |
| 6 | **Redis PoolSize** | 🟡 Medio | 🟢 Bajo | **P2** |
| 7 | **GOGC config** | 🟢 Bajo | 🟢 Bajo | **P2** |
| 8 | **torch.compile** | 🟢 Bajo-Medio | 🟢 Bajo | **P3** |
| 9 | **Async Docling** | 🟡 Medio | 🔴 Alto | **P3** |
| 10 | **Publisher confirms** | 🟢 Bajo | 🟡 Medio | **P3** |
| 11 | **Delayed retry queue** | 🟡 Medio | 🟡 Medio | **P3** |
| 12 | **RabbitMQ channel pool** | 🟡 Medio | 🔴 Alto | **P4** |

---

## Quick Wins — Plan de Implementación Sugerido

Para obtener mejoras inmediatas (1-2 días de trabajo):

1. **Día 1: Optimization #1** (GPU para entities) — 1 hora, impacto crítico
2. **Día 1: Optimization #4** (Prefetch) — 30 minutos, impacto medio
3. **Día 2: Optimization #2** (Batching entities) — 4 horas, impacto alto
4. **Día 2: Optimization #6** (Redis Pool) — 1 hora, impacto medio
5. **Día 2: Optimization #7** (GOGC) — 30 minutos, impacto bajo

**Resultado esperado:** Reducción de ~40-90s a **~15-30s** en pipeline típico (40-65% mejora).

---

## Métricas de Seguimiento

### KPIs a Monitorear

```promql
# Latencia end-to-end
histogram_quantile(0.95, rate(orchestrator_job_duration_seconds_bucket[5m]))

# Throughput
rate(orchestrator_jobs_completed_total[5m])

# Por worker
rate(worker_messages_processed_total[5m])
rate(worker_processing_duration_seconds_sum[5m]) / rate(worker_messages_processed_total[5m])

# Redis
redis_commands_duration_seconds_total{cmd="scan"}  # debería ser 0 tras optimization #5
redis_pool_connections_state{state="idle"}

# GPU utilization
DCGM_FI_DEV_GPU_UTIL{device="1"}  # entities-worker
DCGM_FI_DEV_GPU_UTIL{device="0"}  # embeddings-worker
```

### Dashboard Recommendations

1. **Pipeline Overview**: Jobs completados/minuto, latency p50/p95/p99
2. **Worker Breakdown**: Tiempo por etapa (extraction, embeddings, entities, metadata)
3. **GPU Utilization**: Utilización real vs. disponible
4. **Redis Health**: Pool stats, comandos lentos, SCAN operations
5. **Queue Depth**: Mensajes pendientes por queue

---

## Checklist de Verificación Post-Optimización

```bash
# 1. Benchmark baseline
make test-load  # o script de benchmark existente

# 2. Aplicar quick wins
git checkout -b perf/quick-wins
# implementar optimizations 1, 4, 6, 7

# 3. Benchmark post-optimization
make test-load

# 4. Comparar métricas
# - Latencia p95: antes vs después
# - Throughput: jobs/minuto
# - GPU utilization: %

# 5. Stress test
hey -n 1000 -c 10 http://localhost:8080/v1/documents/process \
  -m POST -H "Content-Type: application/json" \
  -d '{"url": "http://example.com/doc.pdf"}'

# 6. Verificar no hay regresiones
make test
pytest cmd/embeddings-worker/tests/ cmd/entities-worker/tests/ -v
```

---

*Documento generado: 2026-03-30*
*Stack version: Basado en commits hasta 4d74f20*
