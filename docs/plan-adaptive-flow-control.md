# Plan: Adaptive Flow Control para textFlow

## Objetivo

Implementar control de flujo adaptativo en todo el pipeline: desde la ingesta de documentos hasta las llamadas al LLM, maximizando velocidad en hardware potente y manteniendo estabilidad en hardware modesto. Análogo a TCP congestion control aplicado a un pipeline de procesamiento de documentos.

---

## Problema actual

1. **Orchestrator desbocado**: cada POST al endpoint publica inmediatamente en RabbitMQ sin verificar capacidad.
2. **Inferencias estáticas**: los workers usan batch fijo y prefetch fijo, sin adaptarse a la velocidad real del LLM.
3. **Sin límites de colas**: RabbitMQ permite crecimiento ilimitado, causando OOM o timeouts.
4. **Sin rechazo inteligente**: cuando el sistema está saturado, los jobs quedan stuck indefinidamente.

---

## Arquitectura: 3 Válvulas

```
Cliente
  ↓
┌─────────────────────────────────────────┐
│  VALVULA 1: ADMISSION CONTROL           │
│  - Token bucket global (jobs/s)         │
│  - Max concurrent jobs (Redis counter)  │
│  - Queue depth check antes de publicar  │
│  → 503 + Retry-After si saturado       │
└─────────────────────────────────────────┘
  ↓ (si aceptado)
┌─────────────────────────────────────────┐
│  VALVULA 2: RABBITMQ HARD LIMITS        │
│  - x-max-length por cola                │
│  - overflow: reject-publish             │
│  - DLX para mensajes descartados        │
└─────────────────────────────────────────┘
  ↓ (consumidores)
┌─────────────────────────────────────────┐
│  VALVULA 3: ADAPTIVE LLM CONCURRENCY   │
│  - Semáforo AIMD por worker (cwnd)      │
│  - Slow start → additive increase       │
│  - Multiplicative decrease on error     │
│  - Circuit breaker (cooldown tras N     │
│    errores consecutivos)                │
│  - Global Redis semaphore (opcional)    │
└─────────────────────────────────────────┘
  ↓
Resultado
```

---

## Fase 1: Admission Control (Orchestrator)

### 1.1 Variables de entorno

Archivo: `internal/config/config.go`, `.env.example`, `.env`

```bash
# Ingestion admission control
INGESTION_RATE_LIMIT=10          # jobs aceptados por segundo (token bucket)
INGESTION_RATE_BURST=20          # ráfaga máxima
MAX_CONCURRENT_JOBS=30           # jobs en vuelo simultáneos
QUEUE_DEPTH_REJECT_THRESHOLD=500 # rechazar si extract_text > esto
```

### 1.2 Admission Control Handler

Archivo nuevo: `cmd/orchestrator/handlers/admission.go`

```go
type AdmissionController struct {
    redis          *redis.Client
    config         *config.Config
    rateLimiter    *rate.Limiter
}

func (ac *AdmissionController) CanAcceptJob(ctx context.Context) (bool, string, int) {
    // 1. Check rate limit (token bucket global)
    if !ac.rateLimiter.Allow() {
        return false, "rate limit exceeded", 429
    }

    // 2. Check concurrent jobs (Redis counter)
    active, err := ac.redis.SCard(ctx, "{namespace}:active_jobs").Result()
    if err == nil && int(active) >= ac.config.MaxConcurrentJobs {
        return false, "too many jobs in progress", 503
    }

    // 3. Check queue depth
    depth, err := ac.broker.GetQueueInfo(ac.config.ExtractQueue)
    if err == nil && depth.Messages >= ac.config.QueueDepthRejectThreshold {
        return false, "extraction queue saturated", 503
    }

    return true, "", 200
}
```

### 1.3 Integración en el handler de creación de jobs

Archivo: `cmd/orchestrator/main.go` → `createJobHandler`

```go
func (s *Server) createJobHandler(c *gin.Context) {
    // ... validación existente ...

    // NUEVO: Admission control
    canAccept, reason, statusCode := s.admission.CanAcceptJob(c.Request.Context())
    if !canAccept {
        c.Header("Retry-After", "5")
        c.JSON(statusCode, gin.H{
            "error":   reason,
            "message": "System is saturated, please retry later",
        })
        return
    }

    // ... publicación existente ...
}
```

### 1.4 Métricas

- `ia_text_admission_rejected_total` (counter, labels: reason)
- `ia_text_active_jobs_current` (gauge)
- `ia_text_queue_depth_current` (gauge, ya existe parcialmente)

---

## Fase 2: RabbitMQ Hard Limits

### 2.1 Declaración de colas con x-max-length

Archivo: `internal/broker/rabbitmq.go` → `declareQueue`

```go
func (b *RabbitMQBroker) declareQueue(name string) error {
    args := amqp.Table{
        "x-dead-letter-exchange":    "document_processor_dlx",
        "x-dead-letter-routing-key": fmt.Sprintf("%s_failed", name),
        "x-max-length":              int64(b.config.QueueMaxLength),  // NUEVO
        "x-overflow":                "reject-publish",                 // NUEVO
    }
    _, err := b.channel.QueueDeclare(name, true, false, false, false, args)
    return err
}
```

### 2.2 Variables de entorno

```bash
# RabbitMQ queue limits
QUEUE_MAX_LENGTH=1000       # max mensajes por cola antes de reject-publish
QUEUE_MAX_LENGTH_BYTES=0    # 0 = sin límite de bytes (opcional)
```

### 2.3 Rechazo en orchestrator

Cuando `PublishJobMessage` falla por cola llena, el orchestrator debe capturar el error y devolver 503 en lugar de fallar silenciosamente.

---

## Fase 3: Adaptive LLM Concurrency (TCP-like)

### 3.1 Variables de entorno

```bash
# Adaptive LLM concurrency
INFERENCE_ADAPTIVE_ENABLED=true
INFERENCE_MAX_CONCURRENCY=16          # ventana máxima por worker
INFERENCE_MIN_CONCURRENCY=1           # floor
INFERENCE_TARGET_LATENCY_MS=5000      # "el LLM va bien" threshold
INFERENCE_LATENCY_WINDOW=10           # muestras para decidir
INFERENCE_TIMEOUT_DECAY_FACTOR=2      # divisor en multiplicative decrease
INFERENCE_COOLDOWN_SECONDS=30         # circuit breaker cooldown
INFERENCE_CONSECUTIVE_ERRORS_FOR_COOLDOWN=5
INFERENCE_GLOBAL_MAX_CONCURRENCY=0    # 0 = deshabilitado; >0 = global Redis semaphore
```

### 3.2 AdaptiveSemaphore

Archivo nuevo: `cmd/inference-worker/adaptive_semaphore.py`

```python
import threading
import time
import logging

logger = logging.getLogger(__name__)


class AdaptiveSemaphore:
    """
    TCP-like AIMD congestion control for LLM concurrency.
    cwnd starts at min_concurrency, grows additively on success,
    halves on timeout/error.
    """

    def __init__(
        self,
        min_concurrency: int = 1,
        max_concurrency: int = 16,
        target_latency_ms: float = 5000,
        decay_factor: int = 2,
        cooldown_seconds: float = 30,
        consecutive_errors_for_cooldown: int = 5,
    ):
        self._lock = threading.Lock()
        self._cwnd = min_concurrency
        self._min = min_concurrency
        self._max = max_concurrency
        self._target_latency_ms = target_latency_ms
        self._decay_factor = decay_factor
        self._cooldown_seconds = cooldown_seconds
        self._consecutive_errors_threshold = consecutive_errors_for_cooldown

        # State
        self._in_flight = 0
        self._consecutive_errors = 0
        self._cooldown_until = 0.0

        # Metrics
        self._total_requests = 0
        self._total_timeouts = 0
        self._latencies: list = []

    def acquire(self, timeout: float = 30.0) -> bool:
        """Block until a token is available or timeout."""
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                if self._cooldown_until > time.monotonic():
                    # In cooldown, wait
                    pass
                elif self._in_flight < self._cwnd:
                    self._in_flight += 1
                    return True

            if time.monotonic() > deadline:
                return False
            time.sleep(0.05)

    def release(self, latency_ms: float, is_error: bool = False):
        """Release token and update cwnd based on outcome."""
        with self._lock:
            self._in_flight -= 1
            self._total_requests += 1
            self._latencies.append(latency_ms)
            if len(self._latencies) > 100:
                self._latencies = self._latencies[-100:]

            if is_error:
                self._total_timeouts += 1
                self._consecutive_errors += 1
                # Multiplicative decrease
                self._cwnd = max(self._min, self._cwnd // self._decay_factor)
                logger.info(
                    f"cwnd decreased to {self._cwnd} "
                    f"(consecutive errors: {self._consecutive_errors})"
                )

                # Circuit breaker
                if self._consecutive_errors >= self._consecutive_errors_threshold:
                    self._cooldown_until = (
                        time.monotonic() + self._cooldown_seconds
                    )
                    self._consecutive_errors = 0
                    logger.warning(
                        f"Circuit breaker triggered, cooling down "
                        f"for {self._cooldown_seconds}s"
                    )
            else:
                self._consecutive_errors = 0
                # Additive increase if latency is below target
                if latency_ms < self._target_latency_ms:
                    self._cwnd = min(self._max, self._cwnd + 1)
                    logger.info(f"cwnd increased to {self._cwnd}")

    @property
    def cwnd(self) -> int:
        with self._lock:
            return self._cwnd

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    @property
    def is_in_cooldown(self) -> bool:
        return time.monotonic() < self._cooldown_until

    def get_stats(self) -> dict:
        with self._lock:
            avg_latency = (
                sum(self._latencies) / len(self._latencies)
                if self._latencies
                else 0
            )
            return {
                "cwnd": self._cwnd,
                "in_flight": self._in_flight,
                "total_requests": self._total_requests,
                "total_timeouts": self._total_timeouts,
                "avg_latency_ms": round(avg_latency, 1),
                "is_in_cooldown": self.is_in_cooldown,
            }
```

### 3.3 Integración en `_call_llm`

Archivo: `cmd/inference-worker/worker.py`

```python
class InferenceWorker(BaseWorker):
    def __init__(self):
        super().__init__(...)
        self._semaphore = AdaptiveSemaphore(
            min_concurrency=INFERENCE_MIN_CONCURRENCY,
            max_concurrency=INFERENCE_MAX_CONCURRENCY,
            target_latency_ms=INFERENCE_TARGET_LATENCY_MS,
            decay_factor=INFERENCE_TIMEOUT_DECAY_FACTOR,
            cooldown_seconds=INFERENCE_COOLDOWN_SECONDS,
            consecutive_errors_for_cooldown=INFERENCE_CONSECUTIVE_ERRORS_FOR_COOLDOWN,
        )

    def _call_llm(self, prompt: str) -> Optional[Dict]:
        # Acquire token from adaptive semaphore
        if not self._semaphore.acquire(timeout=LLM_TIMEOUT + 10):
            logger.warning("Semaphore acquire timeout, returning empty")
            return None

        t0 = time.monotonic()
        is_error = False
        try:
            response = requests.post(
                f"{LLM_URL}/v1/chat/completions",
                json={...},
                timeout=LLM_TIMEOUT,
            )
            response.raise_for_status()
            return response.json()
        except (requests.Timeout, requests.ConnectionError):
            is_error = True
            raise
        except Exception:
            is_error = True
            raise
        finally:
            latency_ms = (time.monotonic() - t0) * 1000
            self._semaphore.release(latency_ms, is_error=is_error)

    def _export_metrics(self):
        stats = self._semaphore.get_stats()
        self._cwnd_gauge.set(stats["cwnd"])
        self._in_flight_gauge.set(stats["in_flight"])
        self._total_requests_counter.inc(stats["total_requests"])
        self._total_timeouts_counter.inc(stats["total_timeouts"])
        self._avg_latency_gauge.set(stats["avg_latency_ms"])
```

### 3.4 Métricas Prometheus

```python
from prometheus_client import Gauge, Counter

cwnd_gauge = Gauge("inference_worker_cwnd", "Current congestion window")
in_flight_gauge = Gauge("inference_worker_in_flight", "LLM calls currently in flight")
total_requests = Counter("inference_worker_llm_requests_total", "Total LLM requests")
total_timeouts = Counter("inference_worker_llm_timeouts_total", "Total LLM timeouts")
avg_latency = Gauge("inference_worker_llm_avg_latency_ms", "Average LLM latency (ms)")
cooldown_active = Gauge("inference_worker_cooldown", "1 if circuit breaker is active")
```

---

## Fase 4: Cliente con Backpressure

### 4.1 Respetar 503/429

Archivo: `tools/client/main.go`

```go
func (c *Client) submitDocumentWithRetry(req CreateJobRequest) (*CreateJobResponse, error) {
    maxRetries := 5
    for i := 0; i < maxRetries; i++ {
        resp, err := c.doPost("/v1/documents/process", req)
        if err == nil {
            return resp, nil
        }

        if resp.StatusCode == 429 || resp.StatusCode == 503 {
            retryAfter := resp.Header.Get("Retry-After")
            wait, _ := time.ParseDuration(retryAfter + "s")
            if wait == 0 {
                wait = time.Duration(1<<uint(i)) * time.Second // exponential backoff
            }
            log.Printf("Server busy (HTTP %d), waiting %v before retry %d/%d",
                resp.StatusCode, wait, i+1, maxRetries)
            time.Sleep(wait)
            continue
        }

        return nil, fmt.Errorf("HTTP %d: %s", resp.StatusCode, body)
    }
    return nil, fmt.Errorf("max retries exceeded")
}
```

### 4.2 Modo secuencial

```bash
# Un documento a la vez con máximo 1 en vuelo
client -b batch.json -o results.json --max-inflight 1

# O con límite configurable
client -b batch.json -o results.json --max-inflight 3
```

### 4.3 Variables del cliente

```bash
--max-inflight N     # máximo de jobs en vuelo (default: 5)
--sequential         # alias de --max-inflight 1
--retry-backoff sec  # backoff base para reintentos (default: 2s)
```

---

## Fase 5: Tests

### 5.1 Tests de AdaptiveSemaphore

```python
# cmd/inference-worker/tests/test_adaptive_semaphore.py

def test_slow_start_grows():
    sem = AdaptiveSemaphore(min_concurrency=1, max_concurrency=8)
    for _ in range(10):
        sem.acquire(timeout=1)
        sem.release(latency_ms=2000, is_error=False)
    assert sem.cwnd == 8

def test_multiplicative_decrease_on_timeout():
    sem = AdaptiveSemaphore(min_concurrency=1, max_concurrency=16, decay_factor=2)
    sem._cwnd = 16
    sem.acquire(timeout=1)
    sem.release(latency_ms=10000, is_error=True)
    assert sem.cwnd == 8

def test_cooldown_triggers():
    sem = AdaptiveSemaphore(
        min_concurrency=1,
        consecutive_errors_for_cooldown=3,
        cooldown_seconds=10,
    )
    for _ in range(3):
        sem.acquire(timeout=1)
        sem.release(latency_ms=5000, is_error=True)
    assert sem.is_in_cooldown

def test_cwnd_never_goes_below_min():
    sem = AdaptiveSemaphore(min_concurrency=2, max_concurrency=16)
    sem._cwnd = 2
    sem.acquire(timeout=1)
    sem.release(latency_ms=5000, is_error=True)
    assert sem.cwnd >= 2
```

### 5.2 Tests de Admission Control

```go
// cmd/orchestrator/handlers/admission_test.go

func TestRejectWhenConcurrentJobsExceeded(t *testing.T) {
    // Setup: fill active_jobs ZSET with MAX_CONCURRENT_JOBS entries
    // Request should return 503
}

func TestRejectWhenQueueDepthExceeded(t *testing.T) {
    // Setup: simulate deep extract_text queue
    // Request should return 503
}

func TestAcceptWhenSystemHealthy(t *testing.T) {
    // Setup: empty queues, no active jobs
    // Request should return 202
}
```

---

## Archivos a crear/modificar

| Archivo | Acción | Fase |
|---|---|---|
| `cmd/inference-worker/adaptive_semaphore.py` | **CREAR** | 3 |
| `cmd/inference-worker/tests/test_adaptive_semaphore.py` | **CREAR** | 5 |
| `cmd/inference-worker/worker.py` | **MODIFICAR** — importar AdaptiveSemaphore, integrar en _call_llm, métricas | 3 |
| `internal/config/config.go` | **MODIFICAR** — añadir vars admission | 1 |
| `internal/broker/rabbitmq.go` | **MODIFICAR** — x-max-length en colas | 2 |
| `cmd/orchestrator/main.go` | **MODIFICAR** — admission check en createJobHandler | 1 |
| `cmd/orchestrator/handlers/admission.go` | **CREAR** — AdmissionController | 1 |
| `cmd/orchestrator/handlers/admission_test.go` | **CREAR** | 5 |
| `deploy/docker/.env.example` | **MODIFICAR** — nuevas variables | 1-3 |
| `deploy/docker/docker-compose.yml` | **MODIFICAR** — pasar variables a containers | 1-3 |
| `tools/client/main.go` | **MODIFICAR** — backoff + max-inflight + modo secuencial | 4 |
| `AGENTS.md` | **MODIFICAR** — documentar nuevas variables y métricas | 5 |

---

## Variables de entorno resumen

```bash
# ── Fase 1: Admission Control ──
INGESTION_RATE_LIMIT=10
INGESTION_RATE_BURST=20
MAX_CONCURRENT_JOBS=30
QUEUE_DEPTH_REJECT_THRESHOLD=500

# ── Fase 2: RabbitMQ Hard Limits ──
QUEUE_MAX_LENGTH=1000

# ── Fase 3: Adaptive LLM Concurrency ──
INFERENCE_ADAPTIVE_ENABLED=true
INFERENCE_MAX_CONCURRENCY=16
INFERENCE_MIN_CONCURRENCY=1
INFERENCE_TARGET_LATENCY_MS=5000
INFERENCE_LATENCY_WINDOW=10
INFERENCE_TIMEOUT_DECAY_FACTOR=2
INFERENCE_COOLDOWN_SECONDS=30
INFERENCE_CONSECUTIVE_ERRORS_FOR_COOLDOWN=5
INFERENCE_GLOBAL_MAX_CONCURRENCY=0
```

---

## Orden de ejecución

1. **Fase 1** → Adaptar rate limit + admission + queue depth → desbloquea ingesta controlada
2. **Fase 2** → RabbitMQ hard limits → protege broker de OOM
3. **Fase 3** → Adaptive LLM concurrency → resuelve jobs stuck por LLM lento
4. **Fase 4** → Cliente con backoff → cierra el loop completo
5. **Fase 5** → Tests → asegura calidad

---

## Métricas finales del sistema

```text
ingestion:
  - ia_text_admission_rejected_total{reason="rate_limit|concurrent_jobs|queue_depth"}
  - ia_text_active_jobs_current
  - ia_text_queue_depth_current{queue="extract_text|embeddings|entities|metadata|inferences"}

inference:
  - inference_worker_cwnd
  - inference_worker_in_flight
  - inference_worker_llm_requests_total
  - inference_worker_llm_timeouts_total
  - inference_worker_llm_avg_latency_ms
  - inference_worker_cooldown
```

---

## Mejoras críticas (producción intensiva)

### 5.1 Prefetch dinámico alineado con cwnd

**Problema**: Si `prefetch_count=1` en RabbitMQ, el semáforo AIMD jamás crecerá porque el worker nunca tendrá más de un mensaje en memoria.

**Solución**: `prefetch_count = INFERENCE_MAX_CONCURRENCY + 1` para que siempre haya un candidato esperando cuando se libera un token.

```python
# worker.py run()
prefetch_count = int(INFERENCE_MAX_CONCURRENCY) + 1
channel.basic_qos(prefetch_count=prefetch_count)
```

### 5.2 Métrica TTFT en vez de latencia total

**Problema**: La latencia total depende de la longitud de tokens de salida, no necesariamente de saturación del motor. Un chunk largo puede tener latencia alta sin que el LLM esté saturado.

**Solución**: Usar **Time To First Token (TTFT)** o **tokens/s** como métrica de decisión. Si el LLM soporta streaming, medir el tiempo hasta el primer chunk de respuesta. Alternativamente, calcular `tokens_per_second = completion_tokens / latency_ms` y comparar contra un baseline.

```python
def _call_llm(self, prompt):
    t0 = time.monotonic()
    response = requests.post(...)
    latency_ms = (time.monotonic() - t0) * 1000

    # TTFT proxy: si hay streaming, medir primer token
    # Sin streaming: usar tokens/s como proxy
    usage = response.json().get("usage", {})
    completion_tokens = usage.get("completion_tokens", 0)
    tokens_per_sec = (completion_tokens / latency_ms * 1000) if latency_ms > 0 else 0

    self._semaphore.release(
        latency_ms=latency_ms,
        tokens_per_sec=tokens_per_sec,
        is_error=False,
    )
```

En `AdaptiveSemaphore`, cambiar `target_latency_ms` por `target_tokens_per_sec`:
- Si `tokens_per_sec > target` → additive increase
- Si `tokens_per_sec < target / 2` → multiplicative decrease

### 5.3 Graceful shutdown

**Problema**: Si Docker/K8s reinicia el worker, 16 llamadas al LLM en vuelo se cortan bruscamente, generando errores y mensajes en limbo.

**Solución**: Capturar `SIGTERM`, dejar de consumir, esperar `in_flight == 0` con timeout.

```python
import signal

def _handle_sigterm(self, signum, frame):
    self.logger.info("SIGTERM received, graceful shutdown...")
    self._shutdown_requested = True
    if self._channel:
        self._channel.stop_consuming()

    # Wait for in-flight to drain
    timeout = LLM_TIMEOUT + 30
    t0 = time.monotonic()
    while self._semaphore.in_flight > 0 and (time.monotonic() - t0) < timeout:
        time.sleep(0.1)

    if self._semaphore.in_flight > 0:
        self.logger.warning(f"Force shutdown: {self._semaphore.in_flight} still in-flight")
    else:
        self.logger.info("All in-flight requests completed, shutting down")
    sys.exit(0)

signal.signal(signal.SIGTERM, self._handle_sigterm)
```

### 5.4 Anti-poison messages en DLX

**Problema**: Si un mensaje falla persistentemente (supera contexto del LLM), entra en loop infinito entre cola principal y DLX.

**Solución**: Header `x-retry-count` + cola `unrecoverable_errors` tras 3 intentos. Ya existe `_should_retry()` en `base.py` con `MAX_RETRIES=3`, solo hay que asegurar que los mensajes agotados vayan a una cola visible.

```python
# base.py _on_message()
retry_count = int(properties.headers.get("x-retry-count", 0)) if properties.headers else 0
if retry_count >= MAX_RETRIES:
    # Send to unrecoverable_errors queue
    channel.basic_publish(
        exchange="document_processor_dlx",
        routing_key="unrecoverable_errors",
        body=body,
        properties=amqp.BasicProperties(headers={"x-retry-count": retry_count}),
    )
    channel.basic_ack(delivery_tag=method.delivery_tag)
    return
```

### 5.5 KV cache collapse prevention

**Problema**: Si 16 concurrencias con chunks grandes colapsan la VRAM del LLM, empieza a paginar KV cache a RAM, hundiendo el rendimiento.

**Solución**: Calcular `INFERENCE_MAX_CONCURRENCY` basado en VRAM y tamaño de chunk. Documentar fórmula en `.env.example`:

```bash
# VRAM: 16GB = 17179869184 bytes
# max_chunk_tokens: 4096 (configurable)
# bytes_per_token: ~2 (fp16) or ~4 (fp32)
# INFERENCE_MAX_CONCURRENCY = VRAM_bytes / (max_chunk_tokens × 2 × bytes_per_token)
#
# Ejemplos:
#   A40 48GB, chunk 4096, fp16: 48*1024^3 / (4096*2*2) = 288 → limitado a 32 por safety
#   RTX 3090 24GB, chunk 4096, fp16: 24*1024^3 / (4096*2*2) = 144 → limitado a 16
#   Mac M2 16GB unified, chunk 4096: 16*1024^3 / (4096*2*4) = 48 → limitado a 8
INFERENCE_MAX_CONCURRENCY=16
```

---

## Criterio de éxito

1. Un PDF de 254 chunks con LLM a 30s/chunk: jobs completado en ~21 minutos (254 × 30s / 4 workers ≈ 21 min), sin timeout.
2. Oleada de 50 documentos simultáneos: el orchestrator rechaza con 503 cuando el sistema está saturado, el cliente reintenta con backoff.
3. LLM caído: el worker entra en cooldown tras 5 errores, no martillea el endpoint muerto.
4. Hardware potente (LLM a 1s/chunk): `cwnd` crece rápido hasta 16, throughput máximo.
5. Hardware modesto (LLM a 30s/chunk): `cwnd` se queda en 1-2, estabilidad sin timeouts.
