# Roadmap: Document Processing Core

## Visión del Proyecto

Sistema distribuido para procesamiento de documentos con **Go como lenguaje principal** para el orquestador y coordinación. **Python solo para servicios especializados de NLP** (embeddings, NER), ejecutados de forma independiente via message bus.

## Arquitectura General

```
┌─────────────┐
│   Cliente   │
└──────┬──────┘
       │ POST /v1/documents/process
       ▼
┌──────────────────────┐
│  ORQUESTADOR (Go)    │ ← API REST (Gin/Echo)
└──────────┬───────────┘
           │
     ┌─────┴─────┐
     │ RabbitMQ  │ ← Cola de trabajos
     └─────┬─────┘
           │
┌─────────┼─────────┬──────────────┐
│         │         │              │
▼         ▼         ▼              ▼
┌─────────┐ ┌──────────────┐ ┌─────────────┐
│Unstruct- │ │Embeddings    │ │Entities     │
│ured API  │ │Worker (Py)   │ │Worker (Py)  │
└─────────┘ └──────────────┘ └─────────────┘
    │              │              │
    └──────────────┴──────────────┘
                    │
             ┌─────┴─────┐
             │   Redis   │ ← Cache + Estado
             └───────────┘

┌──────────────────────┐
│  Resource Manager   │ ← Detección GPU/CPU
│       (Go)           │   Endpoint: /api/v1/resources
└──────────────────────┘
```

---

## Decisiones Arquitectónicas Clave

- **Lenguaje principal**: Go para orquestación, API, y utilities
- **Python solo para NLP**: Embeddings y NER como workers independientes
- **Sin llamadas Python desde Go**: Los workers son completamente autónomos
- **Resource Manager**: Endpoint HTTP externo para detección GPU (reusable)
- **Unstructured**: Self-hosted vía Docker Compose
- **Prometheus/Grafana**: Externos al proyecto, solo exponer métricas `/metrics`

---

## Stack Tecnológico

| Componente | Tecnología |
|------------|------------|
| Orquestador | Go 1.22 + Gin |
| Workers NLP | Python 3.11 + FastAPI |
| Message Broker | RabbitMQ 3.12 |
| Cache/Estado | Redis 7.x |
| Documentos | Unstructured.io (self-hosted) |
| Resource Detection | Go HTTP endpoint |
| Container runtime | Docker + Docker Compose |
| Observabilidad | Prometheus (externo), endpoint `/metrics` |

---

## Estructura del Proyecto

```
ia-text-orchestrator/
├── cmd/
│   ├── orchestrator/          # Go - API REST principal
│   ├── resource-manager/     # Go - Detección GPU/CPU
│   ├── embeddings-worker/     # Python - Consumer RabbitMQ
│   ├── entities-worker/       # Python - Consumer RabbitMQ
│   └── metadata-worker/       # Python - Consumer RabbitMQ
├── internal/
│   ├── config/                # Configuración unificada
│   ├── models/                # Structs compartidos (Go)
│   ├── broker/                # RabbitMQ client wrapper
│   ├── redis/                 # Redis client wrapper
│   ├── middleware/            # Circuit breaker, logging
│   └── health/                # Health checks compuestos
├── pkg/
│   ├── logging/               # Logger estructurado
│   ├── metrics/               # Prometheus helpers
│   └── tracing/               # OpenTelemetry wrappers
├── deploy/
│   ├── docker/
│   │   ├── docker-compose.yml
│   │   ├── Dockerfile.go
│   │   └── Dockerfile.python
│   └── k8s/
├── scripts/
├── test/
├── go.mod
├── go.sum
├── requirements.txt
└── README.md
```

---

## Roadmap de Implementación

### Fase 0: Fundamentos (Semana 1)

#### 1.1 Estructura del Proyecto
- [ ] Inicializar repo Go con go.mod
- [ ] Crear estructura de directorios
- [ ] Añadir Makefile con comandos estándar
- [ ] Configurar go.mod con dependencias base

#### 1.2 Docker Compose Base
```yaml
# docker-compose.yml
services:
  rabbitmq:
    image: rabbitmq:3.12-management
    ports:
      - "5672:5672"
      - "15672:15672"
    environment:
      RABBITMQ_DEFAULT_USER: guest
      RABBITMQ_DEFAULT_PASS: guest

  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    command: redis-server --appendonly yes

  unstructured:
    image: unstructured-io/unstructured-api:latest
    ports:
      - "8000:8000"
    volumes:
      - ./data:/app/data
```

#### 1.3 Configuración Base (Go)
```go
// internal/config/config.go
type Config struct {
    RabbitMQURL      string `env:"RABBITMQ_URL" default:"amqp://guest:guest@localhost:5672/"`
    RedisURL         string `env:"REDIS_URL" default:"redis://localhost:6379"`
    UnstructuredURL  string `env:"UNSTRUCTURED_URL" default:"http://localhost:8000"`
    ResourceManagerURL string `env:"RESOURCE_MANAGER_URL" default:"http://localhost:9090"`
    HTTPPort         int    `env:"HTTP_PORT" default:"8080"`
    LogLevel         string `env:"LOG_LEVEL" default:"info"`
}
```

#### 1.4 Logger Estructurado
```go
// pkg/logging/logger.go
package logging

import (
    "github.com/rs/zerolog"
    "github.com/rs/zerolog/pkgerrors"
)

func New(level string) zerolog.Logger {
    zerolog.ErrorStackMarshaler = pkgerrors.MarshalStack
    zerolog.TimeFieldFormat = zerolog.TimeFormatUnixMs
    return zerolog.New(zerolog.ConsoleWriter{
        Out: os.Stdout,
    }).Level(parseLevel(level)).With().Timestamp().Logger()
}
```

#### Entregables Fase 0
- [ ] Repo con estructura Go limpia
- [ ] Docker Compose funcional (RabbitMQ, Redis, Unstructured)
- [ ] Configuración via environment variables
- [ ] Makefile con: `make run`, `make build`, `make test`, `make lint`

---

### Fase 1: Orquestador Core + RabbitMQ (Semanas 2-3)

#### 1.1 Modelos de Datos (Go)
```go
// internal/models/job.go
type Job struct {
    ID           string            `json:"id"`
    Status       JobStatus         `json:"status"`
    DocumentURL  string            `json:"document_url,omitempty"`
    DocumentBase64 string          `json:"document_base64,omitempty"`
    Results      *JobResults       `json:"results,omitempty"`
    Error        string            `json:"error,omitempty"`
    CreatedAt    time.Time        `json:"created_at"`
    CompletedAt  *time.Time       `json:"completed_at,omitempty"`
}

type JobStatus string

const (
    StatusPending     JobStatus = "pending"
    StatusExtracting  JobStatus = "extracting"
    StatusProcessing  JobStatus = "processing"
    StatusCompleted   JobStatus = "completed"
    StatusFailed      JobStatus = "failed"
)

type JobResults struct {
    Text       string            `json:"text"`
    Embeddings []float32         `json:"embeddings,omitempty"`
    Entities   []Entity          `json:"entities,omitempty"`
    Metadata   DocumentMetadata  `json:"metadata"`
}

type DocumentMetadata struct {
    MIMEType    string    `json:"mime_type"`
    SizeBytes   int64     `json:"size_bytes"`
    Pages       int       `json:"pages,omitempty"`
}
```

#### 1.2 API REST (Gin)
```go
// cmd/orchestrator/main.go
func main() {
    r := gin.New()
    r.Use(middleware.Logging())
    r.Use(middleware.Recovery())

    // Health check
    r.GET("/health", handlers.HealthHandler)

    // API v1
    v1 := r.Group("/v1")
    {
        v1.POST("/documents/process", handlers.CreateJobHandler)
        v1.GET("/documents/:id", handlers.GetJobHandler)
        v1.DELETE("/documents/:id", handlers.CancelJobHandler)
    }

    // Metrics
    r.GET("/metrics", gin.WrapH(promhttp.Handler()))

    r.Run(":8080")
}
```

#### 1.3 Integración RabbitMQ (Go)
```go
// internal/broker/rabbitmq.go
type Broker struct {
    conn    *amqp.Connection
    channel *amqp.Channel
}

func (b *Broker) Publish(queue string, message interface{}) error {
    body, err := json.Marshal(message)
    if err != nil {
        return err
    }

    return b.channel.Publish(
        "",    // exchange
        queue, // routing key
        false, // mandatory
        false, // immediate
        amqp.Publishing{
            ContentType:  "application/json",
            Body:         body,
            DeliveryMode: amqp.Persistent,
        },
    )
}
```

#### 1.4 Colas y Exchanges
```
Exchange: document_processor (topic)

Colas:
├── extract_text     → Unstructured API
├── embeddings      → embeddings-worker (Python)
├── entities        → entities-worker (Python)
└── metadata        → metadata-worker (Python)
```

#### 1.5 Redis Integration (Go)
```go
// internal/redis/client.go
type RedisClient struct {
    client *redis.Client
}

func (c *RedisClient) SetJobStatus(jobID string, status models.JobStatus) error {
    return c.client.Set(ctx, fmt.Sprintf("job:%s:status", jobID), string(status), 24*time.Hour).Err()
}

func (c *RedisClient) SetJobResults(jobID string, results *models.JobResults) error {
    data, _ := json.Marshal(results)
    return c.client.Set(ctx, fmt.Sprintf("job:%s:results", jobID), data, 24*time.Hour).Err()
}

func (c *RedisClient) GetJobResults(jobID string) (*models.JobResults, error) {
    data, err := c.client.Get(ctx, fmt.Sprintf("job:%s:results", jobID)).Bytes()
    if err != nil {
        return nil, err
    }
    var results models.JobResults
    json.Unmarshal(data, &results)
    return &results, nil
}
```

#### 1.6 Flujo del Orquestador
```
POST /v1/documents/process
  → Generar job_id (UUID)
  → SET job:{id}:status = "pending"
  → Publicar a cola "extract_text"
  → 202 Accepted { job_id, status_url }
```

#### 1.7 Tests Fase 1
- [ ] Tests unitarios para handlers
- [ ] Tests de integración con mocks (RabbitMQ, Redis)
- [ ] Test de carga básica (100 requests concurrentes)

#### Entregables Fase 1
- [ ] Orquestador Go funcional en puerto 8080
- [ ] Endpoints REST: POST /process, GET /{id}, DELETE /{id}
- [ ] Colas RabbitMQ configuradas
- [ ] Redis para estado de jobs (TTL 24h)
- [ ] Tests: >80% coverage

---

### Fase 2: Resource Manager + Workers Python (Semanas 4-5)

#### 2.1 Resource Manager (Go)
```go
// cmd/resource-manager/main.go
type ResourceInfo struct {
    GPUAvailable bool     `json:"gpu_available"`
    GPUDevices   []string `json:"gpu_devices"`
    CPUCores     int      `json:"cpu_cores"`
    MemoryBytes  int64    `json:"memory_bytes"`
}

func main() {
    r := gin.New()
    r.GET("/api/v1/resources", getResourceInfo)
    r.Run(":9090")
}

func getResourceInfo(c *gin.Context) {
    info := ResourceInfo{
        GPUAvailable: detectGPU(),
        GPUDevices:   listGPUDevices(),
        CPUCores:     runtime.NumCPU(),
        MemoryBytes:  getMemory(),
    }
    c.JSON(200, info)
}
```

#### 2.2 Detección GPU
```go
// internal/resources/gpu.go
func detectGPU() bool {
    // Check nvidia-smi
    _, err := exec.LookPath("nvidia-smi")
    if err != nil {
        return false
    }
    output, _ := exec.Command("nvidia-smi", "--query-gpu=count", "--format=csv,noheader").Output()
    count, _ := strconv.Atoi(strings.TrimSpace(string(output)))
    return count > 0
}

func listGPUDevices() []string {
    if !detectGPU() {
        return []string{}
    }
    return []string{"cuda:0"} // Simplificado, expandir para múltiples GPUs
}
```

#### 2.3 Embeddings Worker (Python)
```python
# cmd/embeddings-worker/worker.py
import os
import json
import pika
import numpy as np
from sentence_transformers import SentenceTransformer
import redis
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class EmbeddingsWorker:
    def __init__(self):
        self.redis = redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379"))
        self.resource_url = os.getenv("RESOURCE_MANAGER_URL", "http://localhost:9090")
        self.model = None
        self.batch_size = 32

    def get_resources(self) -> dict:
        import requests
        try:
            resp = requests.get(f"{self.resource_url}/api/v1/resources", timeout=5)
            return resp.json()
        except:
            return {"gpu_available": False}

    def load_model(self):
        resources = self.get_resources()
        device = "cuda:0" if resources.get("gpu_available") else "cpu"
        self.batch_size = 64 if resources.get("gpu_available") else 16
        logger.info(f"Loading model on device: {device}, batch_size: {self.batch_size}")
        self.model = SentenceTransformer("BAAI/bge-m3", device=device)

    def process(self, ch, method, properties, body):
        job_id = json.loads(body)["job_id"]
        logger.info(f"Processing embeddings for job: {job_id}")

        # Get text from Redis
        text = self.redis.get(f"job:{job_id}:text").decode()

        # Generate embeddings
        embeddings = self.model.encode([text], normalize_embeddings=True, batch_size=self.batch_size)
        embeddings_list = embeddings[0].tolist()

        # Store in Redis
        self.redis.set(f"job:{job_id}:embeddings", json.dumps(embeddings_list))

        # Update status
        self.redis.hset(f"job:{job_id}:status", mapping={"embeddings": "completed"})

        ch.basic_ack(delivery_tag=method.delivery_tag)
        logger.info(f"Embeddings completed for job: {job_id}")

def main():
    worker = EmbeddingsWorker()
    worker.load_model()

    connection = pika.BlockingConnection(pika.ConnectionParameters("localhost"))
    channel = connection.channel()

    channel.queue_declare(queue="embeddings", durable=True)
    channel.basic_consume(queue="embeddings", on_message_callback=worker.process)

    logger.info("Embeddings worker started")
    channel.start_consuming()

if __name__ == "__main__":
    main()
```

#### 2.4 Entities Worker (Python)
```python
# cmd/entities-worker/worker.py
import os
import json
import pika
import redis
import logging
from gliner import GLiNER

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class EntitiesWorker:
    def __init__(self):
        self.redis = redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379"))
        self.model = None
        self.default_entities = ["PER", "ORG", "LOC", "DATE", "MONEY"]

    def load_model(self):
        model_path = os.getenv("GLINER_MODEL_PATH", "/models/gliner_model")
        logger.info(f"Loading GLiNER from: {model_path}")
        self.model = GLiNER.from_pretrained(model_path)

    def process(self, ch, method, properties, body):
        job_id = json.loads(body)["job_id"]
        logger.info(f"Processing entities for job: {job_id}")

        text = self.redis.get(f"job:{job_id}:text").decode()
        entities = self.model.predict_entities([text], self.default_entities, threshold=0.8)

        entities_list = [{
            "text": e["text"],
            "label": e["label"],
            "confidence": e["score"],
            "start": e["start"],
            "end": e["end"]
        } for e in entities[0]]

        self.redis.set(f"job:{job_id}:entities", json.dumps(entities_list))
        self.redis.hset(f"job:{job_id}:status", mapping={"entities": "completed"})

        ch.basic_ack(delivery_tag=method.delivery_tag)
        logger.info(f"Entities completed for job: {job_id}")

def main():
    worker = EntitiesWorker()
    worker.load_model()

    connection = pika.BlockingConnection(pika.ConnectionParameters("localhost"))
    channel = connection.channel()
    channel.queue_declare(queue="entities", durable=True)
    channel.basic_consume(queue="entities", on_message_callback=worker.process)

    logger.info("Entities worker started")
    channel.start_consuming()

if __name__ == "__main__":
    main()
```

#### 2.5 Dockerfile Workers Python
```dockerfile
# cmd/embeddings-worker/Dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy worker code
COPY . .

# Download model at build time (optional, reduce startup time)
ENV TRANSFORMERS_CACHE=/models
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-m3')" || true

CMD ["python", "worker.py"]
```

#### 2.6 Requirements Python
```txt
# cmd/embeddings-worker/requirements.txt
sentence-transformers>=2.2.2
torch>=2.0.0
numpy>=1.24.0
pika>=1.3.0
redis>=5.0.0
requests>=2.31.0
prometheus-client>=0.19.0
```

```txt
# cmd/entities-worker/requirements.txt
gliner>=0.2.24
torch>=2.0.0
numpy>=1.21.0
pika>=1.3.0
redis>=5.0.0
requests>=2.31.0
prometheus-client>=0.19.0
```

#### Entregables Fase 2
- [ ] Resource Manager funcionando en puerto 9090
- [ ] Embeddings Worker consume de cola "embeddings"
- [ ] Entities Worker consume de cola "entities"
- [ ] Workers consultan Resource Manager al startup
- [ ] Métricas Prometheus expuestas en `/metrics`

---

### Fase 3: Resiliencia + Observabilidad (Semanas 6-7)

#### 3.1 Circuit Breaker (Go)
```go
// internal/middleware/circuitbreaker.go
import "github.com/sony/gobreaker"

var unstructuredBreaker = gobreaker.NewCircuitBreaker(gobreaker.Settings{
    Name:        "unstructured-api",
    MaxRequests: 3,
    Interval:    30 * time.Second,
    Timeout:     10 * time.Second,
    ReadyToTrip: func(counts gobreaker.Counts) bool {
        failureRatio := float64(counts.TotalFailures) / float64(counts.Requests)
        return counts.Requests >= 3 && failureRatio >= 0.6
    },
})
```

#### 3.2 Retry Logic
```go
// internal/broker/retry.go
func WithRetry(fn func() error, maxRetries int, delay time.Duration) error {
    var lastErr error
    for attempt := 0; attempt < maxRetries; attempt++ {
        if err := fn(); err != nil {
            lastErr = err
            logger.Warnf("Attempt %d failed: %v", attempt+1, err)
            time.Sleep(delay * time.Duration(attempt+1)) // exponential backoff
            continue
        }
        return nil
    }
    return fmt.Errorf("after %d retries: %w", maxRetries, lastErr)
}
```

#### 3.3 Dead Letter Exchange (RabbitMQ)
```go
// Configurar DLX
channel.ExchangeDeclare(
    "document_processor_dlx", // DLX exchange
    "direct",
    true,
    false,
    false,
    false,
    nil,
)

// Colas con DLX
channel.QueueDeclare(
    "extract_text",
    true,
    false,
    false,
    false,
    amqp.Table{
        "x-dead-letter-exchange": "document_processor_dlx",
        "x-dead-letter-routing-key": "extract_text_failed",
    },
)
```

#### 3.4 Métricas Prometheus (Go)
```go
// internal/metrics/metrics.go
var (
    jobsTotal = prometheus.NewCounterVec(
        prometheus.CounterOpts{
            Name: "orchestrator_jobs_total",
            Help: "Total number of jobs processed",
        },
        []string{"status"},
    )
    jobDuration = prometheus.NewHistogramVec(
        prometheus.HistogramOpts{
            Name:    "orchestrator_job_duration_seconds",
            Help:    "Job processing duration",
            Buckets: []float64{0.1, 0.5, 1.0, 2.5, 5.0, 10.0},
        },
        []string{"step"},
    )
    queueLength = prometheus.NewGaugeVec(
        prometheus.GaugeOpts{
            Name: "orchestrator_queue_length",
            Help: "Number of messages in queue",
        },
        []string{"queue"},
    )
)

func init() {
    prometheus.MustRegister(jobsTotal, jobDuration, queueLength)
}
```

#### 3.5 Métricas Prometheus (Python)
```python
# pkg/metrics.py
from prometheus_client import Counter, Histogram, Gauge

jobs_total = Counter('worker_jobs_total', 'Total jobs processed', ['status'])
job_duration = Histogram('worker_job_duration_seconds', 'Job duration', ['worker_type'])
gpu_available = Gauge('worker_gpu_available', 'GPU availability', ['device'])

def init_metrics(worker_type: str):
    def set_gpu_status(status: bool, device: str = "cuda:0"):
        gpu_available.labels(device=device).set(1 if status else 0)
    return {"set_gpu_status": set_gpu_status}
```

#### 3.6 Health Check Compuesto
```go
// internal/health/checker.go
type HealthChecker interface {
    Check() error
}

type CompositeChecker struct {
    checkers []HealthChecker
}

func (c *CompositeChecker) Check() map[string]string {
    results := make(map[string]string)
    for _, checker := range c.checkers {
        if err := checker.Check(); err != nil {
            results[reflect.TypeOf(checker).String()] = err.Error()
        } else {
            results[reflect.TypeOf(checker).String()] = "ok"
        }
    }
    return results
}
```

#### Entregables Fase 3
- [ ] Circuit breaker para Unstructured API
- [ ] Retry con backoff exponencial (max 3 intentos)
- [ ] Dead Letter Queue configurada
- [ ] Métricas Prometheus en `/metrics` (Go + Python)
- [ ] Health check compuesto (RabbitMQ, Redis, Workers)
- [ ] Logs estructurados

---

### Fase 4: Optimización (Semana 8+)

#### 4.1 Cache por Hash de Contenido
```go
// internal/cache/content_cache.go
func (c *Cache) GetOrCompute(key string, compute func() (interface{}, error)) (interface{}, error) {
    // Generar hash del contenido
    hash := sha256.Sum256([]byte(key))

    cacheKey := fmt.Sprintf("content:%x", hash)

    // Check cache
    cached, err := c.client.Get(cacheKey).Bytes()
    if err == nil {
        var result interface{}
        json.Unmarshal(cached, &result)
        return result, nil
    }

    // Compute and cache
    result, err := compute()
    if err != nil {
        return nil, err
    }

    data, _ := json.Marshal(result)
    c.client.Set(cacheKey, data, 7*24*time.Hour) // 7 días para cache de contenido

    return result, nil
}
```

#### 4.2 Rate Limiting
```go
// internal/middleware/ratelimit.go
import "golang.org/x/time/rate"

func RateLimitMiddleware(limit rate.Limit, burst int) gin.HandlerFunc {
    limiter := rate.NewLimiter(limit, burst)

    return func(c *gin.Context) {
        if !limiter.Allow() {
            c.AbortWithStatusJSON(429, gin.H{"error": "rate limit exceeded"})
            return
        }
        c.Next()
    }
}
```

#### 4.3 Procesamiento Paralelo
```go
// En el orquestador, después de extraer texto
func (o *Orchestrator) processInParallel(ctx context.Context, jobID string) {
    var wg sync.WaitGroup

    // Embeddings y entities en paralelo
    wg.Add(2)

    go func() {
        defer wg.Done()
        o.broker.Publish("embeddings", Message{JobID: jobID})
    }()

    go func() {
        defer wg.Done()
        o.broker.Publish("entities", Message{JobID: jobID})
    }()

    wg.Wait()
}
```

#### 4.4 Docker Compose Completo
```yaml
# docker-compose.yml
services:
  orchestrator:
    build: ./cmd/orchestrator
    ports:
      - "8080:8080"
    environment:
      - RABBITMQ_URL=amqp://guest:guest@rabbitmq:5672/
      - REDIS_URL=redis://redis:6379
      - UNSTRUCTURED_URL=http://unstructured:8000
      - RESOURCE_MANAGER_URL=http://resource-manager:9090
    depends_on:
      - rabbitmq
      - redis
      - unstructured
      - resource-manager

  resource-manager:
    build: ./cmd/resource-manager
    ports:
      - "9090:9090"

  embeddings-worker:
    build: ./cmd/embeddings-worker
    environment:
      - REDIS_URL=redis://redis:6379
      - RESOURCE_MANAGER_URL=http://resource-manager:9090
      - RABBITMQ_URL=amqp://guest:guest@rabbitmq:5672/
    depends_on:
      - redis
      - resource-manager

  entities-worker:
    build: ./cmd/entities-worker
    environment:
      - REDIS_URL=redis://redis:6379
      - RABBITMQ_URL=amqp://guest:guest@rabbitmq:5672/
    volumes:
      - ./models:/models
    depends_on:
      - redis

  unstructured:
    image: unstructured-io/unstructured-api:latest
    ports:
      - "8000:8000"
    volumes:
      - ./data:/app/data

  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    command: redis-server --appendonly yes

  rabbitmq:
    image: rabbitmq:3.12-management
    ports:
      - "5672:5672"
      - "15672:15672"
    environment:
      RABBITMQ_DEFAULT_USER: guest
      RABBITMQ_DEFAULT_PASS: guest

networks:
  default:
    name: ia-text-orchestrator
```

#### Entregables Fase 4
- [ ] Cache por hash de contenido (7 días TTL)
- [ ] Rate limiting (configurable por cliente)
- [ ] Procesamiento paralelo de embeddings/entities
- [ ] Docker Compose completo funcional
- [ ] README.md con instrucciones de despliegue

---

## Métricas de Éxito

| Fase | Métrica | Target |
|------|---------|--------|
| 0 | Build time Go | < 30s |
| 1 | Latencia API P99 | < 100ms |
| 2 | Throughput workers | 50 docs/min |
| 3 | Error rate | < 1% |
| 4 | Disponibilidad | 99.9% |

---

## Orden de Implementación Recomendado

1. **Semana 1**: Docker Compose + Estructura Go + Configuración
2. **Semana 2-3**: Orquestador Go + RabbitMQ + Redis
3. **Semana 4**: Resource Manager Go
4. **Semana 5**: Workers Python (Embeddings + Entities)
5. **Semana 6-7**: Resiliencia + Métricas + Health Checks
6. **Semana 8+**: Cache + Rate Limiting + Optimizaciones

---

## Comandos del Proyecto

```bash
# Desarrollo
make run-orchestrator    # Levanta API en puerto 8080
make run-resource        # Resource Manager en puerto 9090
make run-workers         # Levanta todos los workers
make run-all            # Levanta todo con docker-compose

# Tests y Calidad
make test               # Unit tests Go
make test-python        # Unit tests Python
make lint               # golangci-lint
make format             # gofmt + black

# Docker
make docker-build       # Build todas las imágenes
make docker-push        # Push a registry
make docker-logs        # Ver logs de todos los servicios
```

---

## Preguntas Pendientes (para decidir)

1. **Authentication**: ¿Necesita autenticación el API del orquestador o es open-internal?
2. **Data Persistence**: ¿Los documentos se guardan en S3/disco o solo en Redis temporalmente?
3. **Logging centralizado**: ¿Logs a stdout (Kubernetes) o aggregator externo?

---

## FASE 5: Mejoras y Evolución del Sistema (7 semanas)

**Estado:** Fases 0-4 completadas. Sistema funcional con arquitectura base.  
**Objetivo:** Evolucionar de modelo polling a notificaciones push con mejor observabilidad.

### Semana 9: Sistema de Eventos Redis Pub/Sub

**Objetivo:** Base para notificaciones en tiempo real sin polling.

**Tareas:**
1. Crear directorio `internal/events/` con:
   - `event_types.go`: Definir tipos `JobCreated`, `JobProgress`, `JobCompleted`, `JobFailed`
   - `event_bus.go`: Wrapper Redis Pub/Sub con channels
   - `publisher.go`: Interface para publicar eventos

2. Modificar workers Python:
   - embeddings-worker: Publicar evento progreso 33% al completar
   - entities-worker: Publicar evento progreso 66% al completar
   - metadata-worker: Publicar evento progreso 100% + evento `JobCompleted`

3. Actualizar `internal/pipeline/orchestrator.go`:
   - Inicializar EventBus en constructor
   - Publicar eventos en cada transición de estado

**Entregables:**
- [ ] EventBus funcional con Redis Pub/Sub
- [ ] Eventos publicados desde los 3 workers
- [ ] Latencia de eventos < 100ms

---

### Semana 10: Métricas Prometheus y Alerts Críticos

**Objetivo:** Observabilidad completa con alerts críticos.

**Tareas:**
1. Expandir `internal/metrics/metrics.go`:
   - Métricas HTTP: requests_total, latency_seconds
   - Métricas Jobs: jobs_total, job_duration_seconds, jobs_in_progress
   - Métricas Queue: queue_depth, queue_publish_total, queue_consume_total
   - Métricas Redis: redis_latency_seconds, redis_errors_total
   - Métricas RabbitMQ: rabbitmq_errors_total

2. Instrumentar handlers del orchestrator:
   - Middleware para tracking automático de requests
   - Incrementar counters en handlers `createJob`, `getJob`, `deleteJob`

3. Crear módulo métricas Python compartido:
   - Archivo `cmd/metrics_worker.py` con métricas estándar
   - Integrar en los 3 workers
   - Exponer en puertos 8001, 8002, 8003

4. Configurar alerts críticos:
   - Crear `deploy/prometheus/alerts.yml`
   - Alert 1: **WorkerDown** (worker sin actividad 2min)
   - Alert 2: **QueueBlackhole** (cola > 500 mensajes 5min)
   - Alert 3: **RedisDown** (>10 errores Redis 1min)

**Entregables:**
- [ ] 15+ métricas personalizadas activas
- [ ] Endpoints `/metrics` en todos los servicios
- [ ] 3 alerts críticos configurados
- [ ] Prometheus scraping correcto

---

### Semana 11: Sistema de Webhooks

**Objetivo:** Notificaciones HTTP a callbacks configurados con retry automático.

**Tareas:**
1. Crear modelos en `internal/models/webhook.go`:
   - `WebhookConfig`: ID, JobID, URL, Secret, EventTypes, Retries
   - `WebhookPayload`: Event, JobID, Timestamp, Results, Signature
   - `WebhookDeliveryLog`: WebhookID, Attempt, Status, DurationMs

2. Implementar `internal/webhook/dispatcher.go`:
   - Cola de webhooks pendientes
   - Exponential backoff: 5s, 10s, 20s, 40s, 80s, max 5min
   - Máximo 3 reintentos por webhook
   - Firma HMAC-SHA256 para payloads

3. Agregar operaciones Redis:
   - `SaveWebhookConfig()`, `GetWebhookConfig()`
   - `UpdateWebhookStatus()`, `SaveWebhookLog()`
   - TTL 24h para webhooks

4. Crear API endpoints:
   - `POST /v1/webhooks` - Registrar webhook para un job
   - `GET /v1/webhooks/:id` - Ver estado del webhook
   - `DELETE /v1/webhooks/:id` - Cancelar webhook
   - `GET /v1/webhooks/:id/logs` - Ver historial de intentos

5. Integrar Dispatcher con EventBus:
   - Suscribir a eventos `JobCompleted` y `JobFailed`
   - Trigger webhooks automáticamente

**Entregables:**
- [ ] Webhooks entregados en < 30s post-completion
- [ ] Retry automático funcional
- [ ] HMAC signature implementado
- [ ] Logs de intentos almacenados

---

### Semana 12: Server-Sent Events (SSE)

**Objetivo:** Conexión persistente para updates de estado en tiempo real.

**Tareas:**
1. Crear `cmd/orchestrator/handlers/sse_handler.go`:
   - Struct `SSEClient` con ID, JobID, Channel, timestamps
   - Struct `SSEHandler` con gestión de conexiones
   - Límite: 5 conexiones simultáneas por job

2. Implementar endpoint SSE:
   - `GET /v1/documents/:id/stream`
   - Query param: `timeout` (default 5min, max 30min)
   - Sin autenticación (público con job ID)

3. Configuración SSE:
   - Headers: `Content-Type: text/event-stream`, `Cache-Control: no-cache`
   - Heartbeat cada 15 segundos
   - Eventos: `connected`, `progress`, `completed`, `failed`, `timeout`, `heartbeat`

4. Integrar con EventBus:
   - Suscribir a canal `job:{jobID}:events`
   - Forward eventos a clientes SSE conectados
   - Cleanup automático de conexiones huérfanas

5. Configurar CORS:
   - `Access-Control-Allow-Origin: *` para acceso público

**Entregables:**
- [ ] Latencia eventos < 100ms desde Pub/Sub a cliente
- [ ] 50+ conexiones SSE simultáneas soportadas
- [ ] Automatic cleanup de conexiones
- [ ] Heartbeat previene timeouts

---

### Semana 13: Agregación de Resultados

**Objetivo:** Combinar resultados de todos los workers antes de marcar job completo.

**Tareas:**
1. Crear `internal/aggregator/aggregator.go`:
   - Struct `AggregationContext` con JobID, WorkersExpected, WorkersComplete
   - Registro de jobs en progreso
   - Timeout global: 5 minutos

2. Implementar lógica de agregación:
   - Suscribir a eventos de workers (33%, 66%, 100%)
   - Actualizar progreso por worker
   - Verificar cuando todos completaron

3. Combiner de resultados:
   - Método `Combine()` que merge text, embeddings, entities, metadata
   - Construir `JobResults` final solo cuando todos completos

4. Manejo de timeouts:
   - Si timeout y workers incompletos: devolver resultados parciales
   - Incluir warning en metadata
   - Log para investigación

5. Integrar en pipeline:
   - Modificar `ProcessInParallel()` para usar Aggregator
   - Publicar eventos solo en completación real

**Entregables:**
- [ ] Jobs marcados completed solo cuando todos workers terminan
- [ ] Timeout configurable (default 5min)
- [ ] Resultados parciales disponibles en timeout
- [ ] Coordinación correcta entre workers

---

### Semana 14: Tests de Integración

**Objetivo:** Suite completa de tests end-to-end.

**Tareas:**
1. Crear estructura `cmd/orchestrator/tests/`:
   - `integration/pipeline_test.go`
   - `integration/redis_integration_test.go`
   - `integration/rabbitmq_integration_test.go`
   - `integration/webhook_integration_test.go`
   - `e2e/full_job_flow_test.go`
   - `e2e/concurrent_jobs_test.go`

2. Crear fixtures:
   - `fixtures/test_documents/` con PDFs, TXT de prueba
   - Casos: documento simple, vacío, largo (10k chars)

3. Configurar test containers:
   - Redis test container
   - RabbitMQ test container
   - Unstructured mock

4. Implementar test cases:
   - Test flujo completo: POST job → esperar completion → validar resultados
   - Test concurrencia: 10 jobs simultáneos sin race conditions
   - Test Redis operations CRUD
   - Test RabbitMQ publish/consume
   - Test webhook delivery con mock server

5. Assertions:
   - Verificar embeddings, entities, metadata no vacíos
   - Verificar latencias < targets
   - Verificar no hay memory leaks

**Entregables:**
- [ ] 50+ test cases cubriendo flujos principales
- [ ] Test coverage > 70%
- [ ] Tests pasan en < 5 minutos
- [ ] No race conditions en tests concurrentes

---

### Semana 15: Resource Manager Extendido

**Objetivo:** Exponer métricas de colas para autoscaling futuro.

**Tareas:**
1. Agregar endpoints a `cmd/resource-manager/main.go`:
   - `GET /api/v1/queue-status` - Profundidad de colas
   - `GET /api/v1/resources` - Estado completo (GPU, CPU, Memory, Queues)

2. Integración RabbitMQ Management API:
   - Método `getQueueDepth()` que llama a `http://rabbitmq:15672/api/queues`
   - Parsear respuesta para cada cola (embeddings, entities, metadata)

3. Exponer métricas Prometheus:
   - Endpoint `/metrics` en resource-manager
   - Métrica `queue_depth{queue="embeddings"}` para consumo por HPA futuro

4. Struct `ResourceStatus`:
   - AvailableGPU, AvailableCPU, MemoryUsedMB, MemoryTotalMB
   - QueueDepth (embeddings, entities, metadata)
   - ActiveWorkers, Timestamp

5. Documentar endpoints:
   - OpenAPI spec para resource-manager
   - README con ejemplos de uso

**Entregables:**
- [ ] Endpoint queue-status funcional
- [ ] Métricas de cola expuestas correctamente
- [ ] Integración con RabbitMQ Management API
- [ ] Documentación de endpoints

---

## Timeline Actualizado (Fases 0-5)

```
✅ Semanas 1-8:  Fases 0-4 (Sistema base completado)
───────────────────────────────────────────────────
📍 Semana 9:     Sistema de Eventos (Redis Pub/Sub)
📍 Semana 10:    Métricas + 3 Alerts Críticos
📍 Semana 11:    Webhooks con Retry
📍 Semana 12:    Server-Sent Events (SSE)
📍 Semana 13:    Agregación de Resultados
📍 Semana 14:    Tests de Integración
📍 Semana 15:    Resource Manager Extendido
```

**Total:** 15 semanas (~4 meses desde inicio)

---

## Métricas de Éxito Actualizadas

| Fase | Métrica | Target | Status |
|------|---------|--------|--------|
| 0-4 | Sistema base | Funcional | ✅ Completado |
| 5 (S9) | Latencia eventos | < 100ms | 🔄 Pendiente |
| 5 (S10) | Métricas activas | 15+ | 🔄 Pendiente |
| 5 (S11) | Webhook delivery | < 30s | 🔄 Pendiente |
| 5 (S12) | SSE connections | 50+ simultáneas | 🔄 Pendiente |
| 5 (S13) | Agregación timeout | 5min | 🔄 Pendiente |
| 5 (S14) | Test coverage | > 70% | 🔄 Pendiente |
| 5 (S15) | Queue monitoring | Real-time | 🔄 Pendiente |

---

## Dependencias Entre Tareas (Fase 5)

```
Semana 9 (EventBus) → Requerido por Semanas 11, 12, 13
Semana 10 (Métricas) → Independiente, pero útil para todas
Semana 11 (Webhooks) → Requiere Semana 9
Semana 12 (SSE) → Requiere Semana 9
Semana 13 (Agregación) → Requiere Semanas 9, 11, 12
Semana 14 (Tests) → Requiere Semanas 9-13 completadas
Semana 15 (Resource Manager) → Independiente
```

---

## Comandos Actualizados

```bash
# Desarrollo (agregados)
make run-webhook-dispatcher  # Webhook dispatcher standalone
make run-sse-server         # SSE server para testing
make run-aggregator         # Aggregator standalone

# Tests (agregados)
make test-integration       # Tests de integración con containers
make test-e2e              # Tests end-to-end completos
make test-coverage         # Coverage report HTML

# Métricas (nuevos)
make metrics-scrape        # Test Prometheus scrape endpoints
make alerts-validate       # Validar configuración de alerts
```

---

## Decisiones Arquitectónicas - Fase 5

| Decisión | Valor | Razón |
|----------|-------|-------|
| **Webhooks** | Endpoint separado `POST /v1/webhooks` | Permite agregar webhooks post-creation |
| **SSE Auth** | Público (solo job ID) | Simplicidad para MVP |
| **Alerts** | Solo critical (3 alerts) | Evitar alert fatigue |
| **Plataforma** | Docker Compose (sin K8s) | Infraestructura actual, migración futura |
| **EventBus** | Redis Pub/Sub (no RabbitMQ) | Menor latencia para notificaciones |
| **Timeout Agregación** | 5 minutos | Balance entre espera y UX |

---

*Última actualización: 2026-01-29*