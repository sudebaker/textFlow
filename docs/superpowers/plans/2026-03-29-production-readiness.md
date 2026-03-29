# Production Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolver los issues críticos de seguridad, resiliencia, observabilidad y testing para que el sistema sea apto para entornos de producción.

**Architecture:** Microservicios event-driven: orquestador Go (Gin) + workers Python (FastAPI/RabbitMQ) + Redis + RabbitMQ. El plan aplica fixes quirúrgicos a cada capa sin alterar la arquitectura existente.

**Tech Stack:** Go 1.22, Python 3.11, Docker Compose, RabbitMQ 3.12, Redis 7, Prometheus, golangci-lint, pytest, miniredis.

---

## Fases

| Fase | Nombre | Prioridad |
|------|--------|-----------|
| 1 | Seguridad (sin auth) | BLOQUEANTE |
| 2 | Resiliencia crítica | ALTA |
| 3 | Observabilidad | MEDIA |
| 4 | Testing y CI | MEDIA |

---

## FASE 1 — SEGURIDAD

### Task 1.1: Usuarios non-root en Dockerfiles Go

**Contexto:** `orchestrator`, `resource-manager` y `regex-entity-extractor` corren como root. CIS Docker Benchmark exige usuarios sin privilegios.

**Files:**
- Modify: `cmd/orchestrator/Dockerfile`
- Modify: `cmd/resource-manager/Dockerfile`
- Modify: `cmd/regex-entity-extractor/Dockerfile`

- [ ] **Step 1: Abrir y leer cada Dockerfile** para identificar la línea `FROM alpine` de la imagen final.

- [ ] **Step 2: Agregar usuario non-root en `cmd/orchestrator/Dockerfile`**

Insertar antes del `CMD` final:
```dockerfile
RUN addgroup -S appgroup && adduser -S appuser -G appgroup
USER appuser
```

- [ ] **Step 3: Aplicar el mismo cambio a `cmd/resource-manager/Dockerfile`**

Mismo bloque, antes del `CMD`.

- [ ] **Step 4: Aplicar el mismo cambio a `cmd/regex-entity-extractor/Dockerfile`**

Mismo bloque, antes del `CMD`.

- [ ] **Step 5: Verificar que los binarios no necesiten puertos <1024**

```bash
grep -E "EXPOSE [0-9]+" cmd/orchestrator/Dockerfile
grep -E "EXPOSE [0-9]+" cmd/resource-manager/Dockerfile
grep -E "EXPOSE [0-9]+" cmd/regex-entity-extractor/Dockerfile
```
Puertos 8080, 9090, 8081 no requieren root. OK.

- [ ] **Step 6: Build de prueba**

```bash
docker build -t orchestrator-test cmd/orchestrator/
docker run --rm orchestrator-test whoami
```
Esperado: `appuser` (no `root`).

- [ ] **Step 7: Commit**

```bash
git add cmd/orchestrator/Dockerfile cmd/resource-manager/Dockerfile cmd/regex-entity-extractor/Dockerfile
git commit -m "fix(docker): run Go services as non-root user"
```

---

### Task 1.2: Eliminar credenciales por defecto de RabbitMQ

**Contexto:** `docker-compose.yml` y `.env.example` usan `guest:guest`. En producción deben generarse credenciales únicas.

**Files:**
- Modify: `deploy/docker/docker-compose.yml`
- Modify: `deploy/docker/.env.example` (si existe) o `.env.example`

- [ ] **Step 1: Leer `.env.example`** y localizar `RABBITMQ_USER` / `RABBITMQ_PASS`.

- [ ] **Step 2: Cambiar los defaults** de `guest` a placeholders explícitos:

```bash
# En .env.example
RABBITMQ_USER=CHANGE_ME_rabbitmq_user
RABBITMQ_PASS=CHANGE_ME_rabbitmq_pass_min32chars
```

- [ ] **Step 3: Agregar comentario de advertencia** encima de esas líneas:

```bash
# SECURITY: Change these before any deployment. Never use guest:guest in production.
```

- [ ] **Step 4: Verificar docker-compose.yml**

Localizar la sección de RabbitMQ en `deploy/docker/docker-compose.yml`. Las variables deben referenciar env vars, no valores hardcodeados:
```yaml
environment:
  RABBITMQ_DEFAULT_USER: ${RABBITMQ_USER}
  RABBITMQ_DEFAULT_PASS: ${RABBITMQ_PASS}
```
Si hay valores hardcoded, reemplazarlos por las referencias `${}`.

- [ ] **Step 5: Agregar validación de arranque en orchestrator**

En `internal/config/config.go`, añadir validación post-carga:
```go
func (c *Config) Validate() error {
    if strings.Contains(c.RabbitMQURL, "guest:guest") {
        return fmt.Errorf("SECURITY: RabbitMQ default credentials detected. Set RABBITMQ_URL with proper credentials")
    }
    return nil
}
```

Llamar a `Validate()` en `cmd/orchestrator/main.go` justo después de `LoadConfig()`:
```go
cfg, err := config.LoadConfig()
if err != nil { logger.Fatal()... }
if err := cfg.Validate(); err != nil { logger.Fatal().Err(err).Msg("Config validation failed") }
```

- [ ] **Step 6: Commit**

```bash
git add .env.example deploy/docker/docker-compose.yml internal/config/config.go cmd/orchestrator/main.go
git commit -m "fix(security): remove default RabbitMQ credentials and add startup validation"
```

---

### Task 1.3: Corregir CORS wildcard en embeddings-worker

**Contexto:** `embeddings-worker/main.py:148` tiene `allow_origins=["*"]`. Debe restringirse a orígenes conocidos o configurarse vía env var.

**Files:**
- Modify: `cmd/embeddings-worker/main.py`
- Modify: `cmd/embeddings-worker/app/config/settings.py` (o donde esté la config)

- [ ] **Step 1: Leer `cmd/embeddings-worker/main.py`** para ver el bloque CORS completo.

- [ ] **Step 2: Añadir variable de configuración CORS**

En la clase `Settings` de `app/config/settings.py`:
```python
cors_origins: list[str] = Field(default=["http://localhost:8080"], env="CORS_ORIGINS")
```
Si el campo es string separado por comas, usar:
```python
cors_origins_str: str = Field(default="http://localhost:8080", env="CORS_ORIGINS")

@property
def cors_origins(self) -> list[str]:
    return [o.strip() for o in self.cors_origins_str.split(",")]
```

- [ ] **Step 3: Actualizar el bloque CORS en `main.py`**

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Authorization"],
)
```

- [ ] **Step 4: Añadir `CORS_ORIGINS` a `.env.example`**

```bash
# Comma-separated list of allowed origins for CORS
CORS_ORIGINS=http://localhost:8080
```

- [ ] **Step 5: Test manual**

```bash
cd cmd/embeddings-worker
python -c "from app.config.settings import Settings; s = Settings(); print(s.cors_origins)"
```
Esperado: `['http://localhost:8080']`

- [ ] **Step 6: Commit**

```bash
git add cmd/embeddings-worker/main.py cmd/embeddings-worker/app/config/settings.py .env.example
git commit -m "fix(security): restrict CORS origins via env var in embeddings-worker"
```

---

## FASE 2 — RESILIENCIA CRÍTICA

### Task 2.1: Corregir import `time` en embeddings-worker

**Contexto:** `cmd/embeddings-worker/main.py` usa `time.time()` en la línea ~159 pero `import time` aparece al final (~227). Causa `NameError` en runtime.

**Files:**
- Modify: `cmd/embeddings-worker/main.py`

- [ ] **Step 1: Leer el archivo** y localizar todas las ocurrencias de `import time` y usos de `time.`.

- [ ] **Step 2: Mover `import time`** a la sección de stdlib (primera sección de imports, al principio del archivo), respetando el orden alfabético del grupo stdlib definido en `AGENTS.md`.

- [ ] **Step 3: Verificar que no quede duplicado** ningún import de `time` en el resto del archivo.

- [ ] **Step 4: Smoke test**

```bash
cd cmd/embeddings-worker
python -c "import main" 2>&1
```
Esperado: sin `ImportError` ni `NameError`.

- [ ] **Step 5: Commit**

```bash
git add cmd/embeddings-worker/main.py
git commit -m "fix(embeddings-worker): move time import to stdlib section"
```

---

### Task 2.2: Eliminar bare `except:` en extraction-worker

**Contexto:** `cmd/extraction-worker/worker.py` líneas ~549 y ~1038 usan `except:` sin tipo, capturando `SystemExit` y `KeyboardInterrupt`.

**Files:**
- Modify: `cmd/extraction-worker/worker.py`

- [ ] **Step 1: Leer las líneas ~540-560 y ~1030-1045** del archivo para entender el contexto de cada bare except.

- [ ] **Step 2: Reemplazar cada `except:` por `except Exception:`**

```python
# ANTES
except:
    logger.error("...")

# DESPUÉS
except Exception as e:
    logger.error(f"...: {e}")
```

- [ ] **Step 3: Verificar que no haya más bare excepts**

```bash
grep -n "except:" cmd/extraction-worker/worker.py
```
Esperado: sin resultados.

- [ ] **Step 4: Smoke test**

```bash
cd cmd/extraction-worker
python -c "import worker" 2>&1
```

- [ ] **Step 5: Commit**

```bash
git add cmd/extraction-worker/worker.py
git commit -m "fix(extraction-worker): replace bare except with except Exception"
```

---

### Task 2.3: Corregir error suppression crítico en Go orchestrator

**Contexto:** Tres lugares en `cmd/orchestrator/main.go` suprimen errores silenciosamente:
- Líneas ~446-448: `SetJobCreated` failure no propagado
- Líneas ~467-469: EventBus publish failure silenciado
- Líneas ~644-646: `DeleteJob` retorna HTTP 200 aunque falle

**Files:**
- Modify: `cmd/orchestrator/main.go`

- [ ] **Step 1: Leer las líneas 440-480 y 640-650** de `main.go`.

- [ ] **Step 2: Corregir `SetJobCreated` failure (línea ~448)**

```go
// ANTES
if err := redis.SetJobCreated(ctx, job); err != nil {
    logger.Warn().Err(err).Msg("Failed to mark job as created")
}

// DESPUÉS
if err := redis.SetJobCreated(ctx, job); err != nil {
    logger.Error().Err(err).Str("job_id", job.ID).Msg("Failed to mark job as created")
    c.JSON(http.StatusInternalServerError, gin.H{"error": "failed to initialize job"})
    return
}
```

- [ ] **Step 3: Corregir EventBus publish failure (línea ~469)**

```go
// ANTES
if err := eventBus.Publish(ctx, event); err != nil {
    logger.Warn().Err(err).Msg("Failed to publish job event")
}

// DESPUÉS
if err := eventBus.Publish(ctx, event); err != nil {
    logger.Error().Err(err).Str("job_id", job.ID).Msg("Failed to publish job event")
    // No es fatal si el job ya está en Redis — solo registrar
}
```

- [ ] **Step 4: Corregir `DeleteJob` (líneas ~644-646)**

```go
// ANTES
if err := redis.DeleteJob(ctx, jobID); err != nil {
    logger.Error().Msgf("Failed to delete job: %v", err)
}
c.JSON(http.StatusOK, gin.H{...})

// DESPUÉS
if err := redis.DeleteJob(ctx, jobID); err != nil {
    logger.Error().Err(err).Str("job_id", jobID).Msg("Failed to delete job")
    c.JSON(http.StatusInternalServerError, gin.H{"error": "failed to delete job"})
    return
}
c.JSON(http.StatusOK, gin.H{...})
```

- [ ] **Step 5: Compilar para verificar**

```bash
go build ./cmd/orchestrator/...
```
Esperado: sin errores de compilación.

- [ ] **Step 6: Ejecutar tests Redis existentes (no requieren infra)**

```bash
go test -v ./internal/redis/...
```
Esperado: todos PASS.

- [ ] **Step 7: Commit**

```bash
git add cmd/orchestrator/main.go
git commit -m "fix(orchestrator): propagate errors from SetJobCreated and DeleteJob"
```

---

### Task 2.4: Implementar TTL propagation en Redis client

**Contexto:** `internal/redis/client.go` líneas ~110-112, 333-335, 365-367, 399-401 silencian failures de `Expire()`, lo que puede causar memory leaks.

**Files:**
- Modify: `internal/redis/client.go`
- Modify: `internal/redis/client_test.go`

- [ ] **Step 1: Leer las funciones afectadas** (`SetJobStatus`, `SetJobText`, etc.) para entender el flujo.

- [ ] **Step 2: Escribir test que verifique error de TTL**

En `client_test.go`, añadir:
```go
func TestRedisClient_TTLError_IsReturned(t *testing.T) {
    mr := miniredis.RunT(t)
    client, _ := NewRedisClient(mr.Addr(), "test", time.Minute)
    
    // Setear el job status
    err := client.SetJobStatus(context.Background(), "job-1", "processing")
    assert.NoError(t, err)
    
    // Simular fallo de TTL cerrando miniredis
    mr.Close()
    
    // El siguiente set debería retornar error
    err = client.SetJobStatus(context.Background(), "job-1", "done")
    assert.Error(t, err)
}
```

- [ ] **Step 3: Correr el test para ver que falla**

```bash
go test -v ./internal/redis/... -run TestRedisClient_TTLError_IsReturned
```
Esperado: FAIL (el error de Expire se suprime actualmente).

- [ ] **Step 4: Modificar `SetJobStatus` y demás métodos** para propagar el error de `Expire`:

```go
// ANTES
if err := c.client.Expire(ctx, key, c.jobTTL).Err(); err != nil {
    c.logger.Warn().Err(err).Str("key", key).Msg("Failed to set TTL on job status key")
}
return nil

// DESPUÉS
if err := c.client.Expire(ctx, key, c.jobTTL).Err(); err != nil {
    c.logger.Error().Err(err).Str("key", key).Msg("Failed to set TTL on job status key")
    return fmt.Errorf("set TTL for job status: %w", err)
}
return nil
```

Aplicar el mismo patrón en todas las ocurrencias (~110, ~333, ~365, ~399).

- [ ] **Step 5: Correr todos los tests Redis**

```bash
go test -v ./internal/redis/...
```
Esperado: todos PASS incluyendo el nuevo.

- [ ] **Step 6: Compilar**

```bash
go build ./...
```

- [ ] **Step 7: Commit**

```bash
git add internal/redis/client.go internal/redis/client_test.go
git commit -m "fix(redis): propagate TTL errors instead of silencing them"
```

---

### Task 2.5: Graceful shutdown en Python workers

**Contexto:** Todos los workers Python llaman `sys.exit(0)` en el signal handler sin terminar el mensaje en curso. Esto puede dejar jobs en estado `processing` para siempre.

**Files:**
- Modify: `cmd/embeddings-worker/worker.py`
- Modify: `cmd/entities-worker/worker.py`
- Modify: `cmd/extraction-worker/worker.py`
- Modify: `cmd/metadata-worker/worker.py`
- Modify: `cmd/inference-worker/worker.py`
- Modify: `pkg/worker_common/base.py`

- [ ] **Step 1: Leer `pkg/worker_common/base.py`** para ver el patrón base y el signal handler existente.

- [ ] **Step 2: Modificar `base.py` para shutdown graceful**

El patrón correcto es usar un flag de "stopping" y dejar que el loop de consumo termine el mensaje actual:

```python
# En la clase BaseWorker

def __init__(self, ...):
    ...
    self._stopping = False

def _signal_handler(self, signum, frame):
    logger.info(f"Received signal {signum}, initiating graceful shutdown...")
    self._stopping = True
    # No sys.exit() aquí — dejar que el loop actual termine

def _should_stop(self) -> bool:
    return self._stopping

def _on_message_processed(self):
    """Llamar al final de cada callback de mensaje."""
    if self._stopping:
        logger.info("Graceful shutdown: stopping consumer after current message")
        if self._channel and self._channel.is_open:
            self._channel.stop_consuming()
```

- [ ] **Step 3: Actualizar el callback de procesamiento en `base.py`**

Al final del bloque `try/except` del callback principal, llamar a `self._on_message_processed()`.

- [ ] **Step 4: Aplicar el mismo patrón a cada worker individual**

Para cada worker en `cmd/*/worker.py`:
- Eliminar la llamada a `sys.exit(0)` del `signal_handler` local si existe
- Si el worker tiene su propio loop, añadir `if self._stopping: break` al final del loop

- [ ] **Step 5: Verificar que completion-worker tenga signal handler**

Leer `cmd/completion-worker/worker.py` — si no tiene signal handler, añadirlo con el patrón de `base.py`.

- [ ] **Step 6: Test de shutdown**

```bash
# Iniciar un worker en background y enviarle SIGTERM
python cmd/embeddings-worker/worker.py &
PID=$!
sleep 2
kill -SIGTERM $PID
wait $PID
echo "Exit code: $?"
```
Esperado: el proceso termina limpiamente (exit 0) sin mensajes de error.

- [ ] **Step 7: Commit**

```bash
git add pkg/worker_common/base.py cmd/embeddings-worker/worker.py cmd/entities-worker/worker.py \
        cmd/extraction-worker/worker.py cmd/metadata-worker/worker.py \
        cmd/inference-worker/worker.py cmd/completion-worker/worker.py
git commit -m "fix(workers): implement graceful shutdown — drain current message before stopping"
```

---

### Task 2.6: Graceful shutdown en regex-entity-extractor (Go)

**Contexto:** `cmd/regex-entity-extractor/main.go` llama `router.Run(":8081")` directamente sin manejo de señales SIGTERM/SIGINT.

**Files:**
- Modify: `cmd/regex-entity-extractor/main.go`

- [ ] **Step 1: Leer `cmd/regex-entity-extractor/main.go`** — localizar el `router.Run(...)` final.

- [ ] **Step 2: Reemplazar `router.Run` por HTTP server con graceful shutdown**

Seguir el mismo patrón del orchestrator (main.go:131-219). El bloque mínimo:

```go
srv := &http.Server{
    Addr:         ":8081",
    Handler:      router,
    ReadTimeout:  15 * time.Second,
    WriteTimeout: 30 * time.Second,
    IdleTimeout:  120 * time.Second,
}

go func() {
    if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
        logger.Fatal().Err(err).Msg("Server failed")
    }
}()

quit := make(chan os.Signal, 1)
signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)
<-quit
logger.Info().Msg("Shutting down regex-entity-extractor...")

ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
defer cancel()

if err := srv.Shutdown(ctx); err != nil {
    logger.Fatal().Err(err).Msg("Server forced to shutdown")
}
logger.Info().Msg("Server exited")
```

- [ ] **Step 3: Añadir imports necesarios** (`context`, `net/http`, `os/signal`, `syscall`) si no están.

- [ ] **Step 4: Compilar**

```bash
go build ./cmd/regex-entity-extractor/...
```
Esperado: sin errores.

- [ ] **Step 5: Commit**

```bash
git add cmd/regex-entity-extractor/main.go
git commit -m "fix(regex-entity-extractor): implement graceful shutdown on SIGTERM"
```

---

### Task 2.7: Implementar Dead Letter Queue pattern en Python workers

**Contexto:** Todos los workers usan `requeue=True` en el nack, permitiendo retry infinito. El sistema tiene DLX configurado en `rabbitmq.go` pero los workers no lo aprovechan.

**Files:**
- Modify: `pkg/worker_common/base.py`
- Modify: `cmd/embeddings-worker/worker.py`
- Modify: `cmd/entities-worker/worker.py`
- Modify: `cmd/extraction-worker/worker.py`

- [ ] **Step 1: Leer `pkg/worker_common/base.py`** y los workers para identificar todos los `basic_nack(..., requeue=True)`.

- [ ] **Step 2: Añadir contador de reintentos en `base.py`**

Usar la propiedad `x-death` que RabbitMQ añade automáticamente a los headers del mensaje cuando pasa por DLX:

```python
MAX_RETRIES = 3  # configurable via env

def _get_retry_count(self, properties) -> int:
    """Extraer el número de intentos previos desde headers de RabbitMQ."""
    if properties.headers and "x-death" in properties.headers:
        return sum(d.get("count", 0) for d in properties.headers["x-death"])
    return 0

def _should_retry(self, properties) -> bool:
    return self._get_retry_count(properties) < self.max_retries
```

- [ ] **Step 3: Modificar el callback de procesamiento base** para usar retry count:

```python
def _process_message(self, ch, method, properties, body):
    try:
        self._handle_message(ch, method, properties, body)
        ch.basic_ack(delivery_tag=method.delivery_tag)
    except Exception as e:
        logger.error(f"Message processing failed: {e}")
        requeue = self._should_retry(properties)
        if not requeue:
            logger.warning(f"Message exceeded max retries ({self.max_retries}), sending to DLQ")
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=requeue)
```

- [ ] **Step 4: Reemplazar los `basic_nack(requeue=True)` individuales** en cada worker por la lógica centralizada de `base.py`.

- [ ] **Step 5: Añadir `MAX_RETRIES` a `.env.example`**

```bash
# Max message processing retries before sending to dead-letter queue
MAX_RETRIES=3
```

- [ ] **Step 6: Test manual**

```bash
# Verificar que el método existe y retorna el tipo correcto
python -c "
from pkg.worker_common.base import BaseWorker
class MockWorker(BaseWorker):
    def _handle_message(self, ch, method, properties, body): pass
# Solo importar sin instanciar
print('OK')
"
```

- [ ] **Step 7: Commit**

```bash
git add pkg/worker_common/base.py cmd/embeddings-worker/worker.py cmd/entities-worker/worker.py \
        cmd/extraction-worker/worker.py .env.example
git commit -m "fix(workers): replace infinite requeue=True with DLQ pattern using x-death headers"
```

---

### Task 2.8: Redis reconnection logic en workers Python

**Contexto:** Todos los workers crean el `redis_client` en `__init__` sin lógica de reconexión. Si Redis se cae y vuelve, el worker queda inutilizable.

**Files:**
- Modify: `pkg/worker_common/base.py`

- [ ] **Step 1: Leer cómo se crea el cliente Redis** en `base.py` y en los workers individuales.

- [ ] **Step 2: Añadir método `_get_redis` con lazy reconnect** en `base.py`:

```python
import redis as redis_lib
from tenacity import retry, stop_after_attempt, wait_exponential

@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    reraise=True,
)
def _connect_redis(self) -> redis_lib.Redis:
    client = redis_lib.from_url(self.redis_url, decode_responses=True)
    client.ping()  # Valida la conexión
    return client

def _get_redis(self) -> redis_lib.Redis:
    """Retorna el cliente Redis, reconectando si es necesario."""
    if self._redis_client is None:
        self._redis_client = self._connect_redis()
    try:
        self._redis_client.ping()
    except redis_lib.ConnectionError:
        logger.warning("Redis connection lost, reconnecting...")
        self._redis_client = self._connect_redis()
    return self._redis_client
```

- [ ] **Step 3: Verificar que `tenacity` está en los requirements**

```bash
grep -r "tenacity" cmd/*/requirements.txt pkg/requirements.txt 2>/dev/null
```
Si no está, añadirlo a `pkg/requirements.txt` o a cada worker que lo necesite.

- [ ] **Step 4: Reemplazar accesos directos a `self._redis_client`** por `self._get_redis()` en `base.py`.

- [ ] **Step 5: Test de imports**

```bash
python -c "from pkg.worker_common.base import BaseWorker; print('OK')"
```

- [ ] **Step 6: Commit**

```bash
git add pkg/worker_common/base.py pkg/requirements.txt
git commit -m "fix(workers): add Redis reconnection logic with exponential backoff"
```

---

## FASE 3 — OBSERVABILIDAD

### Task 3.1: Health checks en docker-compose para todos los servicios

**Contexto:** 9 de 11 servicios en `docker-compose.yml` no tienen health check configurado, lo que hace que `depends_on: condition: service_healthy` sea no fiable.

**Files:**
- Modify: `deploy/docker/docker-compose.yml`

- [ ] **Step 1: Leer `deploy/docker/docker-compose.yml`** completo, identificar servicios sin `healthcheck`.

- [ ] **Step 2: Añadir health check al `orchestrator`**

```yaml
healthcheck:
  test: ["CMD", "wget", "--quiet", "--tries=1", "--spider", "http://localhost:8080/health"]
  interval: 15s
  timeout: 5s
  retries: 3
  start_period: 10s
```

- [ ] **Step 3: Añadir health check al `resource-manager`**

```yaml
healthcheck:
  test: ["CMD", "wget", "--quiet", "--tries=1", "--spider", "http://localhost:9090/health"]
  interval: 15s
  timeout: 5s
  retries: 3
  start_period: 10s
```

- [ ] **Step 4: Añadir health check a cada Python worker**

Los workers exponen `/health` vía FastAPI. Usar `curl` o `wget` según lo disponible en la imagen:

```yaml
# Para embeddings-worker (puerto metrics 8001)
healthcheck:
  test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8001/health')"]
  interval: 30s
  timeout: 10s
  retries: 3
  start_period: 60s  # Los workers tardan en cargar modelos ML
```

Repetir para `entities-worker` (8002), `metadata-worker` (8003), `extraction-worker` (8004), `completion-worker` (8005), `inference-worker` (8006).

- [ ] **Step 5: Corregir la indentación en línea ~214-215** (bug detectado en el análisis):

Verificar que `DEDUPLICATION_ENABLED=false` y `ENTITIES_DEVICE` tengan el mismo nivel de indentación que el resto de las variables del bloque `environment`.

- [ ] **Step 6: Verificar sintaxis del docker-compose**

```bash
docker compose -f deploy/docker/docker-compose.yml config --quiet
```
Esperado: sin errores.

- [ ] **Step 7: Commit**

```bash
git add deploy/docker/docker-compose.yml
git commit -m "fix(docker): add health checks to all services and fix indentation bug"
```

---

### Task 3.2: Habilitar Redis Exporter en Prometheus

**Contexto:** `deploy/prometheus/prometheus.yml` tiene el scrape de Redis comentado. Sin él no hay visibilidad del estado de Redis.

**Files:**
- Modify: `deploy/prometheus/prometheus.yml`
- Modify: `deploy/docker/docker-compose.yml`

- [ ] **Step 1: Leer `deploy/prometheus/prometheus.yml`** para ver el bloque Redis comentado.

- [ ] **Step 2: Añadir `redis-exporter` al docker-compose**

```yaml
redis-exporter:
  image: oliver006/redis_exporter:latest
  environment:
    REDIS_ADDR: "redis://redis:6379"
    REDIS_PASSWORD: ${REDIS_PASSWORD:-}
  ports:
    - "9121:9121"
  networks:
    - datastore
    - backend
  depends_on:
    redis:
      condition: service_healthy
  restart: unless-stopped
  deploy:
    resources:
      limits:
        cpus: "0.2"
        memory: 128M
```

- [ ] **Step 3: Descomentar/añadir el scrape job en prometheus.yml**

```yaml
- job_name: 'redis'
  static_configs:
    - targets: ['redis-exporter:9121']
  scrape_interval: 15s
```

- [ ] **Step 4: Verificar sintaxis**

```bash
docker compose -f deploy/docker/docker-compose.yml config --quiet
```

- [ ] **Step 5: Commit**

```bash
git add deploy/prometheus/prometheus.yml deploy/docker/docker-compose.yml
git commit -m "feat(observability): enable Redis exporter for Prometheus scraping"
```

---

### Task 3.3: Propagar job_id en logs de workers Python

**Contexto:** Los workers loguean con el job_id solo en algunos casos. Para correlacionar logs cross-service, debe aparecer en todos los logs relacionados con un job.

**Files:**
- Modify: `pkg/worker_common/base.py`
- Modify: `pkg/logging_python.py`

- [ ] **Step 1: Leer `pkg/logging_python.py`** para ver `JobLogger`.

- [ ] **Step 2: Verificar que `JobLogger` añade `job_id` a todos los log records**

Si no lo hace, modificar el `StructuredFormatter` para incluir `job_id` del contexto cuando esté disponible.

- [ ] **Step 3: En `base.py`, asegurarse de que el callback de procesamiento** usa `JobLogger` con el job_id extraído del mensaje:

```python
def _process_message(self, ch, method, properties, body):
    job_id = self._extract_job_id(body)  # método a implementar
    job_logger = JobLogger(self.logger, job_id) if job_id else self.logger
    job_logger.info("Starting message processing")
    ...
```

- [ ] **Step 4: Implementar `_extract_job_id`** que parsea el body JSON sin fallar:

```python
def _extract_job_id(self, body: bytes) -> str | None:
    try:
        data = json.loads(body)
        return data.get("job_id") or data.get("id")
    except Exception:
        return None
```

- [ ] **Step 5: Correr los tests de Python existentes para verificar no hay regresión**

```bash
pytest cmd/embeddings-worker/tests/ cmd/entities-worker/tests/ -v
```
Esperado: los tests que pasaban antes siguen pasando.

- [ ] **Step 6: Commit**

```bash
git add pkg/worker_common/base.py pkg/logging_python.py
git commit -m "feat(observability): propagate job_id in all Python worker log records"
```

---

## FASE 4 — TESTING Y CI

### Task 4.1: Arreglar tests de RabbitMQ que cuelgan

**Contexto:** `internal/broker/rabbitmq_test.go` tiene 2 tests que intentan conectar a RabbitMQ real y se quedan colgados. Deben usar un mock o un server AMQP embebido.

**Files:**
- Modify: `internal/broker/rabbitmq_test.go`

- [ ] **Step 1: Leer `internal/broker/rabbitmq_test.go`** completo para identificar los tests problemáticos y qué función llaman.

- [ ] **Step 2: Verificar si hay una librería de mock AMQP disponible**

```bash
grep -r "amqptest\|amqp.*mock\|vektra/amqp" go.mod go.sum 2>/dev/null
```

Si no, añadir `github.com/Azure/go-amqp` o usar la estrategia de inyección de dependencias.

- [ ] **Step 3: Refactorizar los tests problemáticos** para usar un timeout corto o skip si no hay AMQP disponible:

```go
func TestRabbitMQBroker_PublishReconnectOnNilChannel(t *testing.T) {
    if os.Getenv("RABBITMQ_URL") == "" {
        t.Skip("Skipping: RABBITMQ_URL not set (requires real RabbitMQ)")
    }
    // ... resto del test
}
```

- [ ] **Step 4: Alternativamente, añadir un timeout global** a los tests de broker:

```go
func TestMain(m *testing.M) {
    // Timeout global de 10s para el suite de broker tests
    done := make(chan int, 1)
    go func() {
        done <- m.Run()
    }()
    select {
    case code := <-done:
        os.Exit(code)
    case <-time.After(10 * time.Second):
        fmt.Println("TIMEOUT: broker tests exceeded 10s — likely waiting for RabbitMQ")
        os.Exit(1)
    }
}
```

- [ ] **Step 5: Verificar que los tests corren sin colgarse**

```bash
timeout 30 go test -v -timeout 15s ./internal/broker/... 2>&1
```
Esperado: tests con skip o fail rápido, nunca colgados.

- [ ] **Step 6: Actualizar CI para separar tests con y sin infra**

En `.github/workflows/ci.yml`, asegurarse de que los tests de broker que requieren infra estén en un job con `RABBITMQ_URL` configurado o marcados con build tag.

- [ ] **Step 7: Commit**

```bash
git add internal/broker/rabbitmq_test.go .github/workflows/ci.yml
git commit -m "fix(tests): skip RabbitMQ integration tests when no AMQP server available"
```

---

### Task 4.2: Arreglar dependencias faltantes en tests de entities-worker

**Contexto:** Los tests de `cmd/entities-worker/tests/test_api.py` fallan con `ImportError: uvicorn not found`.

**Files:**
- Modify: `cmd/entities-worker/requirements.txt` (o `requirements-dev.txt` si existe)

- [ ] **Step 1: Leer `cmd/entities-worker/requirements.txt`** y cualquier `requirements-dev.txt`.

- [ ] **Step 2: Verificar qué dependencies faltan**

```bash
cd cmd/entities-worker
python -m pytest tests/test_api.py -v 2>&1 | grep "ModuleNotFoundError\|ImportError" | head -20
```

- [ ] **Step 3: Añadir dependencies de test** a un archivo `requirements-test.txt`:

```
uvicorn>=0.24.0
httpx>=0.25.0  # Para TestClient de FastAPI
pytest>=7.4.0
pytest-asyncio>=0.21.0
```

- [ ] **Step 4: Instalar y verificar**

```bash
pip install -r cmd/entities-worker/requirements-test.txt
pytest cmd/entities-worker/tests/test_api.py -v
```
Esperado: los 5 tests pasan.

- [ ] **Step 5: Actualizar el CI** para instalar `requirements-test.txt` en el job `python-tests`:

```yaml
- name: Install dependencies
  run: |
    python -m pip install --upgrade pip setuptools wheel
    find cmd/*/requirements.txt -exec pip install -r {} \;
    find cmd/*/requirements-test.txt -exec pip install -r {} \; 2>/dev/null || true
    pip install pytest pytest-cov
```

- [ ] **Step 6: Commit**

```bash
git add cmd/entities-worker/requirements-test.txt .github/workflows/ci.yml
git commit -m "fix(ci): add missing test dependencies for entities-worker"
```

---

### Task 4.3: Añadir tests para middleware Go

**Contexto:** `internal/middleware/` (circuit breaker, retry, rate limit) no tiene tests. Son componentes críticos de resiliencia.

**Files:**
- Create: `internal/middleware/circuitbreaker_test.go`
- Create: `internal/middleware/ratelimit_test.go`
- Create: `internal/middleware/retry_test.go`

- [ ] **Step 1: Escribir tests del circuit breaker** (`circuitbreaker_test.go`):

```go
package middleware_test

import (
    "errors"
    "testing"
    "github.com/stretchr/testify/assert"
    "ia-text-orchestrator/internal/middleware"
)

func TestCircuitBreaker_ClosedByDefault(t *testing.T) {
    cb := middleware.NewCircuitBreaker("test", middleware.DefaultConfig())
    assert.Equal(t, "closed", cb.State())
}

func TestCircuitBreaker_OpensAfterFailures(t *testing.T) {
    cb := middleware.NewCircuitBreaker("test", middleware.CircuitBreakerConfig{
        MaxRequests:   3,
        ReadyToTrip:   func(counts middleware.Counts) bool { return counts.ConsecutiveFailures >= 3 },
        Timeout:       1,
    })
    
    failFn := func() error { return errors.New("fail") }
    
    for i := 0; i < 3; i++ {
        cb.Execute(failFn)
    }
    
    assert.Equal(t, "open", cb.State())
}

func TestCircuitBreaker_BlocksWhenOpen(t *testing.T) {
    // ... similar
}

func TestCircuitBreaker_TransitionsToHalfOpen(t *testing.T) {
    // ... similar con time mock
}
```

- [ ] **Step 2: Escribir tests del rate limiter** (`ratelimit_test.go`):

```go
func TestRateLimiter_AllowsWithinLimit(t *testing.T) { ... }
func TestRateLimiter_BlocksWhenExceeded(t *testing.T) { ... }
func TestRateLimiter_Returns429(t *testing.T) { ... }
```

- [ ] **Step 3: Escribir tests del retry** (`retry_test.go`):

```go
func TestRetry_SucceedsOnFirstTry(t *testing.T) { ... }
func TestRetry_RetriesOnFailure(t *testing.T) { ... }
func TestRetry_RespectsMaxRetries(t *testing.T) { ... }
func TestRetry_ExponentialBackoff(t *testing.T) { ... }
```

- [ ] **Step 4: Correr los nuevos tests**

```bash
go test -v ./internal/middleware/...
```
Esperado: todos PASS.

- [ ] **Step 5: Commit**

```bash
git add internal/middleware/circuitbreaker_test.go internal/middleware/ratelimit_test.go \
        internal/middleware/retry_test.go
git commit -m "test(middleware): add unit tests for circuit breaker, rate limiter, and retry"
```

---

## Checklist de Verificación Final

Antes de declarar el sistema production-ready, verificar:

```bash
# 1. Build completo
make build

# 2. Tests Go (sin infra)
go test -timeout 30s ./internal/redis/... ./internal/middleware/...

# 3. Tests Python
pytest cmd/embeddings-worker/tests/ cmd/entities-worker/tests/ -v

# 4. Lint Go
make lint

# 5. Format
make format

# 6. Docker build sin errores
make docker-build

# 7. Verificar no-root
docker run --rm orchestrator whoami  # debe retornar 'appuser', no 'root'

# 8. Validar docker-compose
docker compose -f deploy/docker/docker-compose.yml config --quiet

# 9. Verificar que ningún Dockerfile tiene credenciales hardcodeadas
grep -r "guest" cmd/*/Dockerfile deploy/
```

---

## Resumen de Tareas

| # | Tarea | Fase | Estimado | Archivos clave |
|---|-------|------|----------|----------------|
| 1.1 | Non-root en Dockerfiles Go | 1 | 30 min | 3x Dockerfile |
| 1.2 | Eliminar credenciales por defecto | 1 | 45 min | .env.example, docker-compose.yml, config.go |
| 1.3 | Corregir CORS wildcard | 1 | 30 min | embeddings-worker/main.py |
| 2.1 | Fix import time | 2 | 15 min | embeddings-worker/main.py |
| 2.2 | Eliminar bare except | 2 | 20 min | extraction-worker/worker.py |
| 2.3 | Error suppression Go | 2 | 45 min | orchestrator/main.go |
| 2.4 | TTL propagation Redis | 2 | 45 min | redis/client.go + test |
| 2.5 | Graceful shutdown Python | 2 | 1.5h | base.py + 5 workers |
| 2.6 | Graceful shutdown regex extractor | 2 | 30 min | regex-entity-extractor/main.go |
| 2.7 | Dead Letter Queue pattern | 2 | 1h | base.py + 3 workers |
| 2.8 | Redis reconnection | 2 | 45 min | base.py |
| 3.1 | Health checks docker-compose | 3 | 30 min | docker-compose.yml |
| 3.2 | Redis Exporter Prometheus | 3 | 20 min | prometheus.yml, docker-compose.yml |
| 3.3 | Propagar job_id en logs | 3 | 30 min | base.py, logging_python.py |
| 4.1 | Fix tests RabbitMQ | 4 | 45 min | rabbitmq_test.go |
| 4.2 | Fix deps entities tests | 4 | 20 min | requirements-test.txt, ci.yml |
| 4.3 | Tests middleware Go | 4 | 2h | 3x test files |

**Total estimado: ~12 horas de trabajo.**
