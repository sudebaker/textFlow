# Plan de Revisión y Mejoras - IA Text Orchestrator

## Resumen Ejecutivo

Se han identificado **87 problemas** en el proyecto, categorizados en 4 niveles de severidad:
- **15 CRÍTICOS (P0)**: Pérdida de datos, vulnerabilidades de seguridad, crashes
- **22 ALTOS (P1)**: Reliability, observabilidad deficiente
- **28 MEDIOS (P2)**: Performance degradado, tech debt
- **22 BAJOS (P3)**: Optimizaciones menores

**Estimación total**: 15-22 días de trabajo (3-4.5 semanas para equipo de 2-3 devs)

---

## FASE 1: CRÍTICO (P0) - 3-5 días ⚠️

### 1.1 Redis Eviction Policy - DATA CORRUPTION (30min)

**Problema**: `allkeys-lru` puede eliminar jobs en progreso
```yaml
# docker-compose.yml:28 - ACTUAL (PELIGROSO)
command: redis-server --maxmemory-policy allkeys-lru
```

**Solución**:
```yaml
command: redis-server --appendonly yes --maxmemory 512mb --maxmemory-policy noeviction
```

**Archivos**: `deploy/docker/docker-compose.yml:28`

**Validación**: Verificar que Redis no evicciona keys con `INFO stats`

---

### 1.2 Secrets Hardcoded - SEGURIDAD CRÍTICA (2-4h)

**Problema**: Credenciales en plaintext en múltiples archivos
- `internal/config/config.go:15` - `guest:guest` hardcoded
- `deploy/docker/docker-compose.yml:11-12,64,117,168,208-209` - Secrets en env vars

**Solución**:
1. Crear `.env.example` (sin secrets)
2. Usar Docker secrets o variables de entorno
3. Remover defaults inseguros del código

```go
// config.go - REMOVER default con credenciales
RabbitMQURL string `env:"RABBITMQ_URL"` // SIN default
```

```yaml
# docker-compose.yml - Usar env vars
services:
  rabbitmq:
    environment:
      RABBITMQ_DEFAULT_USER: ${RABBITMQ_USER}
      RABBITMQ_DEFAULT_PASS: ${RABBITMQ_PASS}
```

**Archivos**:
- `internal/config/config.go`
- `deploy/docker/docker-compose.yml`
- Crear `.env.example`

**Validación**: `git grep -i "guest:guest"` debe retornar 0 resultados

---

### 1.3 Memory Leak en RateLimiter (1-2h)

**Problema**: Map crece infinitamente sin cleanup
```go
// internal/middleware/ratelimit.go:26-44
func (rl *RateLimiter) getLimiter(key string) *rate.Limiter {
    // NUNCA elimina entries viejas del map
    rl.limiters[key] = limiter  // MEMORY LEAK
}
```

**Solución**:
```go
type limiterEntry struct {
    limiter  *rate.Limiter
    lastSeen time.Time
}

// Agregar cleanup goroutine
func (rl *RateLimiter) cleanup(ctx context.Context) {
    ticker := time.NewTicker(5 * time.Minute)
    defer ticker.Stop()

    for {
        select {
        case <-ctx.Done():
            return
        case <-ticker.C:
            rl.cleanupOldEntries(1 * time.Hour)
        }
    }
}
```

**Archivos**: `internal/middleware/ratelimit.go`

**Validación**: Test que verifique que el map no crece indefinidamente

---

### 1.4 Goroutine Leaks (3-4h)

**Problema 1**: Consumer RabbitMQ sin cancelación
```go
// internal/broker/rabbitmq.go:154-163
func (b *RabbitMQBroker) Consume(...) {
    go func() {
        for msg := range msgs {  // Goroutine nunca termina
            // ...
        }
    }()
}
```

**Problema 2**: Server HTTP sin cleanup
```go
// cmd/orchestrator/main.go:74-78
go func() {
    if err := srv.ListenAndServe(); err != nil {
        logger.Fatal().Msgf("Server error: %v", err)  // NO ejecuta defers
    }
}()
```

**Solución**:
```go
// Usar context cancelation
func (b *RabbitMQBroker) Consume(ctx context.Context, ...) error {
    go func() {
        for {
            select {
            case <-ctx.Done():
                return
            case msg, ok := <-msgs:
                if !ok { return }
                // process...
            }
        }
    }()
}

// Usar errgroup
import "golang.org/x/sync/errgroup"

g, ctx := errgroup.WithContext(context.Background())
g.Go(func() error {
    return srv.ListenAndServe()
})
```

**Archivos**:
- `internal/broker/rabbitmq.go`
- `cmd/orchestrator/main.go`

**Validación**: Test con race detector `go test -race`

---

### 1.5 Validación de Entrada - DoS/SSRF (2-3h)

**Problema**: Sin validación de tamaño ni URLs
```go
// cmd/orchestrator/main.go:197-203
// NO valida: tamaño de DocumentBase64, seguridad de DocumentURL
```

**Solución**:
```go
const MaxDocumentSize = 10 * 1024 * 1024  // 10MB

func validateDocumentInput(req *models.CreateJobRequest) error {
    if req.DocumentBase64 != "" {
        decoded, err := base64.StdEncoding.DecodeString(req.DocumentBase64)
        if err != nil {
            return fmt.Errorf("invalid base64")
        }
        if len(decoded) > MaxDocumentSize {
            return fmt.Errorf("document too large: %d bytes", len(decoded))
        }
    }

    if req.DocumentURL != "" {
        u, err := url.Parse(req.DocumentURL)
        if err != nil {
            return fmt.Errorf("invalid URL")
        }

        // Prevenir SSRF
        if ip := net.ParseIP(u.Hostname()); ip != nil {
            if ip.IsLoopback() || ip.IsPrivate() {
                return fmt.Errorf("private IP not allowed")
            }
        }

        // Whitelist de schemes
        if u.Scheme != "http" && u.Scheme != "https" {
            return fmt.Errorf("scheme not allowed: %s", u.Scheme)
        }
    }

    return nil
}
```

**Archivos**: `cmd/orchestrator/main.go`

**Validación**: Tests con payloads maliciosos (SSRF, documentos grandes)

---

### 1.6 RabbitMQ DLX No Implementado (1-2h)

**Problema**: Exchange DLX declarado en config pero no existe
```go
// internal/broker/rabbitmq.go:94
"x-dead-letter-exchange": "document_processor_dlx",  // NO EXISTE
```

**Solución**:
```go
func (b *RabbitMQBroker) declareDLX() error {
    // 1. Crear exchange
    err := b.channel.ExchangeDeclare(
        "document_processor_dlx",
        "topic",
        true,  // durable
        false, false, false, nil,
    )
    if err != nil {
        return err
    }

    // 2. Crear DLQ
    _, err = b.channel.QueueDeclare(
        "dead_letters",
        true, false, false, false, nil,
    )
    if err != nil {
        return err
    }

    // 3. Bind
    return b.channel.QueueBind(
        "dead_letters",
        "*_failed",
        "document_processor_dlx",
        false, nil,
    )
}
```

**Archivos**: `internal/broker/rabbitmq.go`

**Validación**: Forzar un mensaje a DLQ y verificar que llega

---

### 1.7 Redis URL Parsing Roto (1h)

**Problema**: Parsing no funciona con redis:// URLs
```go
// internal/redis/client.go:47-57
func getAddrFromURL(url string) string {
    return url  // BUG: Devuelve "redis://host" en vez de "host:port"
}
```

**Solución**:
```go
func New(cfg *config.Config) (*RedisClient, error) {
    opt, err := redis.ParseURL(cfg.RedisURL)  // Usar parser oficial
    if err != nil {
        return nil, fmt.Errorf("invalid Redis URL: %w", err)
    }

    client := redis.NewClient(opt)
    // ...
}
```

**Archivos**: `internal/redis/client.go`

**Validación**: Test con URLs `redis://user:pass@host:6379/0`

---

### 1.8 Pika Connection Params Incorrectos (1h)

**Problema**: Workers Python usan `url` como parámetro
```python
# cmd/metadata-worker/worker.py:176
pika.ConnectionParameters(url, ...)  # INCORRECTO - url no es parámetro válido
```

**Solución**:
```python
def parse_rabbitmq_url(url: str) -> pika.ConnectionParameters:
    parsed = urllib.parse.urlparse(url)
    credentials = pika.PlainCredentials(
        parsed.username or 'guest',
        parsed.password or 'guest'
    )

    return pika.ConnectionParameters(
        host=parsed.hostname or 'localhost',
        port=parsed.port or 5672,
        virtual_host=parsed.path[1:] if parsed.path else '/',
        credentials=credentials,
        heartbeat=600,
        blocked_connection_timeout=300
    )
```

**Archivos**:
- `cmd/metadata-worker/worker.py`
- `cmd/embeddings-worker/worker.py`
- `cmd/entities-worker/worker.py`

**Validación**: Verificar que workers se conectan correctamente

---

### 1.9 Docker Images Sin Versión (30min)

**Problema**: Uso de `:latest` tag
```yaml
# docker-compose.yml
image: unstructured-api:latest       # MAL
image: prom/prometheus:latest        # MAL
image: grafana/grafana:latest        # MAL
```

**Solución**:
```yaml
unstructured:
  image: quay.io/unstructured-io/unstructured-api:0.0.66

prometheus:
  image: prom/prometheus:v2.48.0

grafana:
  image: grafana/grafana:10.2.3
```

**Archivos**: `deploy/docker/docker-compose.yml`

**Validación**: `grep -r ":latest" deploy/` debe retornar 0 resultados

---

## FASE 2: RELIABILITY (P1) - 4-6 días

### 2.1 Alertas Prometheus Rotas (2h)

**Problema**: Alertas usan métricas inexistentes
```yaml
# deploy/prometheus/alerts.yml:8-9
ia_text_worker_jobs_total         # NO EXISTE (es ia_text_jobs_total)
ia_text_worker_jobs_in_progress   # NO EXISTE
```

**Solución**:
1. Corregir nombres de métricas en alertas
2. Implementar actualización de `ia_text_queue_depth`

```go
// En orchestrator/main.go
go func() {
    ticker := time.NewTicker(15 * time.Second)
    for range ticker.C {
        mqBroker.UpdateQueueMetrics()  // NUEVO
    }
}()
```

**Archivos**:
- `deploy/prometheus/alerts.yml`
- `internal/broker/rabbitmq.go` (agregar UpdateQueueMetrics)
- `cmd/orchestrator/main.go`

**Validación**: Prometheus UI → Alerts, verificar que no hay errores

---

### 2.2 Healthchecks Inefectivos (2-3h)

**Problema**: Healthchecks superficiales o faltantes
- Orchestrator: Solo verifica Redis ping, no RabbitMQ ni queues
- Workers Python: Sin healthcheck HTTP
- Docker: Healthchecks faltantes o con intervals largos

**Solución**:
1. Healthcheck detallado en Go con latency y detalles
2. Agregar endpoint `/health` a workers Python
3. Configurar Docker healthchecks

```go
// internal/health/checker.go (CREAR)
type HealthStatus struct {
    Status    string                 `json:"status"`
    Checks    map[string]CheckResult `json:"checks"`
}

func (hc *HealthChecker) Check(ctx context.Context) *HealthStatus {
    // Verificar Redis con latency
    // Verificar RabbitMQ connection
    // Verificar queue depths
    // ...
}
```

**Archivos**:
- `internal/health/checker.go` (CREAR)
- `cmd/orchestrator/main.go`
- Todos los workers Python
- `deploy/docker/docker-compose.yml`

**Validación**: `curl http://localhost:8080/health` retorna status detallado

---

### 2.3 Network Security (1-2h)

**Problema**: Todos los servicios en misma red, puertos expuestos
```yaml
# docker-compose.yml:217-221
networks:
  ia-text-network:  # Una sola red para todo
```

**Solución**:
```yaml
networks:
  frontend:    # API pública
    internal: false
  backend:     # Servicios internos
    internal: true
  datastore:   # Redis/RabbitMQ
    internal: true

services:
  orchestrator:
    networks: [frontend, backend, datastore]
    ports: ["8080:8080"]  # Solo este expuesto

  rabbitmq:
    networks: [datastore]
    # NO exponer 5672 externamente

  prometheus:
    ports: ["127.0.0.1:9091:9090"]  # Bind a localhost
```

**Archivos**: `deploy/docker/docker-compose.yml`

**Validación**: Verificar que RabbitMQ/Redis no son accesibles desde exterior

---

### 2.4 Resource Limits (1h)

**Problema**: Límites incorrectos o faltantes
- RabbitMQ: Sin límites → OOM risk
- Redis: 256MB muy bajo
- Orchestrator: 512MB insuficiente

**Solución**:
```yaml
services:
  orchestrator:
    deploy:
      resources:
        limits: {cpus: '2', memory: 1G}
        reservations: {cpus: '0.5', memory: 512M}

  redis:
    command: redis-server --maxmemory 1gb --maxmemory-policy noeviction
    deploy:
      resources:
        limits: {cpus: '1', memory: 1.5G}
        reservations: {memory: 1G}

  rabbitmq:
    deploy:
      resources:
        limits: {cpus: '1', memory: 1G}
        reservations: {memory: 512M}
```

**Archivos**: `deploy/docker/docker-compose.yml`

**Validación**: `docker stats` muestra límites correctos

---

### 2.5 Timeouts Mal Configurados (2-3h)

**Problema**: Sin timeouts en HTTP server, Redis, contexts

**Solución**:
```go
// HTTP server
srv := &http.Server{
    Addr:           addr,
    Handler:        r,
    ReadTimeout:    15 * time.Second,
    WriteTimeout:   30 * time.Second,
    IdleTimeout:    120 * time.Second,
    MaxHeaderBytes: 1 << 20,
}

// Redis client
client := redis.NewClient(&redis.Options{
    Addr:         addr,
    DialTimeout:  5 * time.Second,
    ReadTimeout:  3 * time.Second,
    WriteTimeout: 3 * time.Second,
    PoolTimeout:  4 * time.Second,
})

// Context en handlers
func createJobHandler(c *gin.Context) {
    ctx, cancel := context.WithTimeout(c.Request.Context(), 30*time.Second)
    defer cancel()
    // Usar ctx en todas las operaciones
}
```

**Archivos**:
- `cmd/orchestrator/main.go`
- `internal/redis/client.go`
- Workers Python

**Validación**: Tests de timeout con operaciones lentas

---

### 2.6 Prefetch Count Subóptimo (1h)

**Problema**: `prefetch_count=1` limita throughput

**Solución**:
```python
# Configurar por worker según carga
PREFETCH_COUNT = int(os.getenv("PREFETCH_COUNT", "5"))
channel.basic_qos(prefetch_count=PREFETCH_COUNT)
```

**Workers**:
- Embeddings: 5 (GPU puede procesar batch)
- Entities: 5
- Metadata: 10 (más ligero)

**Archivos**: Todos los workers

**Validación**: Métricas de throughput antes/después

---

### 2.7 Métricas Faltantes (2-3h)

**Problema**: Sin métricas de runtime, queue lag, cache hits

**Solución**:
```go
// pkg/metrics/metrics.go
var (
    GoroutineCount = promauto.NewGauge(...)
    MemoryAllocBytes = promauto.NewGauge(...)
    JobStepDuration = promauto.NewHistogramVec(...)  // por step
    QueueConsumerLag = promauto.NewGaugeVec(...)
    CacheHits/CacheMisses = promauto.NewCounterVec(...)
)

// Collector goroutine
func StartMetricsCollector() {
    go func() {
        ticker := time.NewTicker(10 * time.Second)
        for range ticker.C {
            GoroutineCount.Set(float64(runtime.NumGoroutine()))
            // ...
        }
    }()
}
```

**Archivos**: `pkg/metrics/metrics.go`

**Validación**: Prometheus UI muestra nuevas métricas

---

## FASE 3: PERFORMANCE (P2) - 5-7 días

### 3.1 Redis Key Namespacing (1-2h)

**Problema**: Keys sin namespace, colisiones posibles

**Solución**:
```go
type RedisClient struct {
    namespace string  // "orchestrator" o env-specific
}

func (c *RedisClient) key(parts ...string) string {
    allParts := append([]string{c.namespace}, parts...)
    return strings.Join(allParts, ":")
}

// Uso: orchestrator:job:123:status
```

**Archivos**: `internal/redis/client.go`

---

### 3.2 Context Cancelation (2-3h)

**Problema**: Operations no respetan context.Done()

**Solución**: Propagar context en todas las operaciones, verificar cancelación

**Archivos**:
- `internal/broker/rabbitmq.go`
- `cmd/orchestrator/main.go`

---

### 3.3 Batch Processing (2-3h)

**Problema**: Workers procesan 1 mensaje a la vez

**Solución**: Implementar batch processor con buffer y timeout

**Archivos**: Todos los workers

---

### 3.4 Test Coverage (3-5 días)

**Problema**: 0% coverage actual

**Objetivo**:
- Unit tests Go: 70% coverage
- Unit tests Python: 60% coverage
- Integration tests: Flujos críticos
- E2E: 1 test completo

**Archivos**: Crear `test/` directory, tests en cada package

---

## FASE 4: TECH DEBT (P3) - 3-4 días

### 4.1 Code Duplication (1-2 días)

**Objetivo**: Crear shared library para workers Python

**Archivos**: Crear `pkg/worker_common/`

---

### 4.2 Performance Optimizations (1 día)

- JSON encoding pool
- Redis pipelining
- Connection pooling
- Reduce allocations

---

## Archivos Críticos a Modificar

**Prioridad MÁXIMA** (P0):
1. `deploy/docker/docker-compose.yml` - Redis eviction, secrets, versions, limits
2. `internal/config/config.go` - Secrets hardcoded
3. `internal/middleware/ratelimit.go` - Memory leak
4. `cmd/orchestrator/main.go` - Goroutine leaks, validación, timeouts
5. `internal/broker/rabbitmq.go` - DLX, goroutine leaks
6. `internal/redis/client.go` - URL parsing
7. `cmd/metadata-worker/worker.py` - Pika params
8. `cmd/embeddings-worker/worker.py` - Pika params
9. `cmd/entities-worker/worker.py` - Pika params

**Prioridad ALTA** (P1):
10. `deploy/prometheus/alerts.yml` - Métricas incorrectas
11. `pkg/metrics/metrics.go` - Métricas faltantes
12. `internal/health/checker.go` - CREAR
13. Workers Python - Healthcheck endpoints

---

## Estrategia de Implementación

### Sprint 1 (Semana 1) - P0 URGENTE
**Días 1-2**:
- 1.2 Redis eviction (30min) ← EMPEZAR AQUÍ
- 1.1 Secrets (2-4h)
- 1.9 Docker versions (30min)
- 1.5 Validación input (2-3h)

**Días 3-4**:
- 1.3 RateLimiter leak (1-2h)
- 1.4 Goroutine leaks (3-4h)
- 1.7 Redis URL parsing (1h)

**Día 5**:
- 1.6 RabbitMQ DLX (1-2h)
- 1.8 Workers Pika (1h)
- Deploy y validación

### Sprint 2 (Semana 2) - P1 RELIABILITY
- Métricas, alertas, healthchecks
- Network security, resource limits
- Timeouts, prefetch

### Sprint 3 (Semana 3) - P2 PERFORMANCE
- Namespacing, context, batch
- **Test coverage (3-5 días)**

### Sprint 4 (Semana 4) - P3 REFINAMIENTO
- Code deduplication
- Performance optimizations

---

## Verificación End-to-End

### Después de Fase 1 (P0):
```bash
# 1. Levantar servicios
docker-compose up -d

# 2. Crear job
curl -X POST http://localhost:8080/v1/documents/process \
  -H "Content-Type: application/json" \
  -d '{"document_base64": "SGVsbG8gV29ybGQ="}'

# Verificar:
# - Job se crea correctamente
# - No hay goroutine leaks (prometheus)
# - Redis no evicciona keys
# - Workers se conectan correctamente
# - Healthchecks responden

# 3. Verificar seguridad
git grep -i "guest:guest"  # Debe retornar 0
curl -X POST http://localhost:8080/v1/documents/process \
  -d '{"document_url": "http://localhost/admin"}'  # Debe fallar

# 4. Verificar métricas
curl http://localhost:8080/metrics | grep ia_text

# 5. Verificar alertas
curl http://localhost:9091/api/v1/alerts  # Sin errores
```

### Después de Fase 2 (P1):
```bash
# Healthchecks
curl http://localhost:8080/health  # Status detallado

# Alertas funcionales
# Verificar en Prometheus UI que alertas se evalúan

# Network security
telnet localhost 5672  # Debe fallar (no expuesto)
telnet localhost 6379  # Debe fallar (no expuesto)

# Resource limits
docker stats  # Verificar límites aplicados
```

### Después de Fase 3 (P2):
```bash
# Tests
go test -race -cover ./...
python -m pytest --cov

# Performance
# Benchmarks de throughput con/sin batch processing
```

---

## Métricas de Éxito

**Seguridad**:
- [ ] 0 secrets hardcoded
- [ ] Validación al 100%
- [ ] Network segregado

**Reliability**:
- [ ] 0 memory leaks
- [ ] 0 goroutine leaks
- [ ] Healthchecks funcionando
- [ ] Alertas sin errores

**Performance**:
- [ ] Throughput > 100 jobs/min
- [ ] Latency p95 < 2s
- [ ] Memory estable

**Testing**:
- [ ] 70% code coverage Go
- [ ] 60% code coverage Python
- [ ] CI pipeline verde

---

## Rollback Strategy

Cada cambio crítico debe tener plan de rollback:

1. **Cambios de configuración**: Mantener `.old` backup
2. **Code changes**: Git revert + redeploy
3. **Docker compose**: `docker-compose -f docker-compose.old.yml up`
4. **Secrets**: Mantener secrets actuales como fallback temporal

**Deploy gradual**: Staging → Canary (10%) → Full production
