# Orchestrator Improvements Design

**Fecha:** 2026-03-31  
**Estado:** Aprobado  
**Proyecto:** ia-text-orchestrator  

---

## Contexto

El orquestador Go expone una API REST (puerto 8080) que coordina workers Python vía RabbitMQ/Redis. Se identificaron 6 mejoras en tres fases para hacer la integración con sistemas externos (ej. ocugraphrag) más robusta, eficiente y observable.

### Estado Actual (Pre-Mejoras)

| Característica | Estado |
|---------------|--------|
| Health check detallado | ✅ Ya implementado |
| Métricas Prometheus | ✅ Ya implementado |
| Entity offsets | ⚠️ Existen en modelo interno pero se pierden al deduplicar |
| Webhooks | ⚠️ Solo vía env var global, no por request |
| Streaming SSE | ❌ No existe (EventBus Redis sí existe) |
| Batch processing | ❌ No existe |
| OpenAPI schema | ❌ No existe para el orchestrator |
| Compresión embeddings | ❌ No existe |

---

## Fase 1: Quick Wins

### 1. Entity Offsets en Respuesta

**Problema:** `EntityMinimal` pierde `start_offset`, `end_offset` y `chunk_id` al deduplicar en completion-worker, obligando a sistemas externos a usar fuzzy matching.

**Solución:** Ampliar `EntityMinimal` preservando el offset del primer match durante deduplicación.

**Cambio en `internal/models/job.go`:**
```go
// Antes
type EntityMinimal struct {
    Label      string  `json:"label"`
    Text       string  `json:"text"`
    Confidence float32 `json:"confidence"`
}

// Después
type EntityMinimal struct {
    Label       string  `json:"label"`
    Text        string  `json:"text"`
    Confidence  float32 `json:"confidence"`
    StartOffset int     `json:"start_offset"`
    EndOffset   int     `json:"end_offset"`
    ChunkID     string  `json:"chunk_id,omitempty"`
}
```

**Cambio en `cmd/completion-worker/worker.py`:**
- Al deduplicar entidades, preservar `start`, `end`, `chunk_id` del primer match
- Mapear campos del worker (`start`, `end`) a nombres del response (`start_offset`, `end_offset`)

**Respuesta final:**
```json
{
  "entities": {
    "PERSON": [
      {
        "text": "Juan Pérez",
        "label": "PERSON",
        "confidence": 0.95,
        "start_offset": 0,
        "end_offset": 11,
        "chunk_id": "chunk_001"
      }
    ]
  }
}
```

**Compatibilidad:** Backwards compatible (campos adicionales en JSON).

---

### 2. Webhooks por Request

**Problema:** El webhook solo puede configurarse globalmente vía `WEBHOOK_URL` env var, impidiendo que distintos clientes reciban notificaciones individuales.

**Solución:** Aceptar `webhook_url` y `webhook_secret` opcionales en el body de cada request.

**Cambio en request (`internal/models/job.go`):**
```go
type ProcessRequest struct {
    // campos existentes...
    WebhookURL    string `json:"webhook_url,omitempty"`
    WebhookSecret string `json:"webhook_secret,omitempty"`
}
```

**Almacenamiento:** Guardar en Redis como parte de los metadatos del job:
- `orchestrator:job:{id}:webhook_url`
- `orchestrator:job:{id}:webhook_secret`

**Firma HMAC:** Si se proporciona `webhook_secret`, el completion-worker incluirá:
```
X-Webhook-Signature: sha256=<hmac-sha256(secret, body)>
X-Webhook-Timestamp: <unix-timestamp>
```

**Payload de notificación:**
```json
{
  "job_id": "uuid",
  "status": "completed|failed",
  "completed_at": "2026-03-31T10:30:00Z",
  "download_url": "http://orchestrator:8080/v1/documents/{id}/download",
  "error": null
}
```

**Prioridad:** Webhook por request tiene prioridad sobre `WEBHOOK_URL` global.

---

### 3. OpenAPI Schema con gin-swagger

**Problema:** No existe documentación formal del API del orchestrator, dificultando la integración y el mantenimiento.

**Solución:** Integrar `swaggo/gin-swagger` con comentarios de anotación en handlers.

**Dependencias nuevas:**
```
github.com/swaggo/gin-swagger
github.com/swaggo/files
github.com/swaggo/swag (herramienta CLI)
```

**Endpoints documentados:**
- `POST /v1/documents/process`
- `POST /v1/documents/upload`
- `GET /v1/documents/:id`
- `GET /v1/documents/:id/download`
- `DELETE /v1/documents/:id`
- `GET /health`
- `GET /metrics`
- `GET /v1/jobs/:id/stream` (Fase 2)
- `POST /v1/documents/batch` (Fase 2)
- `GET /v1/batches/:id/status` (Fase 2)

**Nuevo endpoint:**
```
GET /swagger/index.html  → UI interactiva
GET /swagger/doc.json    → Schema descargable
```

**Generación:**
```bash
swag init -g cmd/orchestrator/main.go -o docs/swagger
```

---

## Fase 2: Features

### 4. Streaming SSE

**Problema:** Los clientes deben hacer polling agresivo para conocer el progreso del job.

**Solución:** Nuevo endpoint SSE que reusa el EventBus Redis existente.

**Nuevo endpoint:**
```
GET /v1/jobs/:id/stream
```

**Headers de respuesta:**
```
Content-Type: text/event-stream
Cache-Control: no-cache
Connection: keep-alive
X-Accel-Buffering: no
```

**Eventos:**
```
event: job_created
data: {"job_id": "uuid", "status": "pending", "timestamp": "..."}

event: job_progress
data: {"job_id": "uuid", "step": "extracting|embedding|entities|metadata|inferences", "progress": 0.0-1.0, "timestamp": "..."}

event: job_completed
data: {"job_id": "uuid", "status": "completed", "download_url": "...", "timestamp": "..."}

event: job_failed
data: {"job_id": "uuid", "status": "failed", "error": "...", "timestamp": "..."}

: heartbeat (cada 30s para mantener conexión viva)
```

**Implementación en Go:**
```go
// cmd/orchestrator/handlers/stream.go
func StreamJobHandler(c *gin.Context) {
    jobID := c.Param("id")
    
    // Verificar que el job existe
    // Suscribir a canal Redis: job:{jobID}:events
    // Stream eventos con c.Stream()
    // Cerrar cuando: job completa/falla o cliente desconecta
}
```

**Manejo de conexión:**
- Timeout máximo: 10 minutos
- Heartbeat cada 30 segundos
- Si el job ya completó al conectar: enviar evento final inmediatamente y cerrar

---

### 5. Batch Processing

**Problema:** Para ingestas masivas hay que crear N requests individuales y hacer polling N veces.

**Solución:** Endpoint batch que crea jobs individuales, los agrupa con un `batch_id` y notifica cuando todos completan.

**Request:**
```
POST /v1/documents/batch
```
```json
{
  "documents": [
    {"text": "...", "filename": "doc1.pdf"},
    {"text": "...", "filename": "doc2.pdf", "metadata": {...}}
  ],
  "max_concurrency": 4,
  "webhook_url": "http://ocugraphrag/webhooks/batch",
  "webhook_secret": "hmac-secret"
}
```

**Response (202 Accepted):**
```json
{
  "batch_id": "batch-uuid",
  "total": 2,
  "jobs": [
    {"id": "job-uuid-1", "filename": "doc1.pdf", "status": "pending"},
    {"id": "job-uuid-2", "filename": "doc2.pdf", "status": "pending"}
  ],
  "status_url": "/v1/batches/batch-uuid/status",
  "created_at": "2026-03-31T10:30:00Z"
}
```

**Estado del batch:**
```
GET /v1/batches/:batch_id/status
```
```json
{
  "batch_id": "batch-uuid",
  "status": "running|completed|partial|failed",
  "total": 2,
  "completed": 1,
  "failed": 0,
  "pending": 1,
  "jobs": [
    {"id": "job-uuid-1", "status": "completed"},
    {"id": "job-uuid-2", "status": "processing"}
  ],
  "created_at": "...",
  "completed_at": null
}
```

**Estados de batch:**
- `running`: al menos 1 job en progreso
- `completed`: todos los jobs completaron con éxito
- `partial`: todos terminaron, algunos fallaron
- `failed`: todos fallaron

**Redis keys:**
- `orchestrator:batch:{id}:meta` → total, created_at, webhook_url, webhook_secret
- `orchestrator:batch:{id}:jobs` → set de job IDs
- `orchestrator:batch:{id}:status` → estado actual

**Webhook de batch:** Se dispara cuando el último job (éxito o falla) completa:
```json
{
  "batch_id": "batch-uuid",
  "status": "completed|partial|failed",
  "total": 2,
  "completed": 2,
  "failed": 0,
  "jobs": [...]
}
```

**max_concurrency:** Limita cuántos jobs se crean simultáneamente (default: 10, max: 50).

---

## Fase 3: Optimizaciones

### 6. Compresión de Embeddings

**Problema:** Embeddings grandes generan alta latencia de transferencia. 100 chunks × 1024 dims × 4 bytes = ~400KB sin comprimir.

**Solución:** Query param opcional en el endpoint de descarga.

**Request:**
```
GET /v1/documents/:id/download?compression=gzip
```

**Response con compresión:**
```json
{
  "job_id": "uuid",
  "compression": "gzip",
  "chunks": [
    {
      "chunk_id": "chunk_001",
      "text": "...",
      "start_offset": 0,
      "end_offset": 512,
      "embedding": "H4sIAAAAAAAAA3P0dH..."
    }
  ]
}
```

**Sin compresión (default):**
```json
{
  "job_id": "uuid",
  "compression": null,
  "chunks": [
    {
      "chunk_id": "chunk_001",
      "text": "...",
      "embedding": [0.123, 0.456, ...]
    }
  ]
}
```

**Implementación:**
- El embedding se almacena como MsgPack en Redis (ya existe)
- Al servir: si `?compression=gzip`, comprimir bytes con `compress/gzip` y encodear a base64
- El cliente descomprime: `base64_decode → gzip_decompress → float32 array`

**Ratio esperado:** ~10:1 para datos numéricos de embeddings.

---

## Impacto en Componentes

| Componente | Fase 1 | Fase 2 | Fase 3 |
|-----------|--------|--------|--------|
| `internal/models/job.go` | ✏️ EntityMinimal + ProcessRequest | ✏️ BatchRequest/Response | - |
| `cmd/orchestrator/main.go` | ✏️ rutas swagger | ✏️ rutas batch + stream | ✏️ param compresión |
| `cmd/orchestrator/handlers/` | ✏️ anotaciones swagger | ✏️ StreamHandler, BatchHandler | ✏️ DownloadHandler |
| `internal/redis/client.go` | ✏️ webhook keys | ✏️ batch keys | - |
| `cmd/completion-worker/worker.py` | ✏️ preservar offsets, webhook por-job | ✏️ notificar batch | - |
| `docs/swagger/` | ✨ nuevo | ✏️ nuevos endpoints | - |
| `go.mod` / `go.sum` | ✏️ gin-swagger | - | - |

---

## Consideraciones de Compatibilidad

- **Fase 1:** Totalmente backwards compatible. Campos nuevos en JSON son ignorados por clientes antiguos.
- **Fase 2:** Endpoints nuevos. Sin breaking changes.
- **Fase 3:** Query param opcional. Sin breaking changes.
- **Deployment:** Cada fase se puede desplegar independientemente.

---

## Métricas de Éxito

- **Offsets:** Sistema externo puede ubicar entidades sin fuzzy matching
- **Webhooks:** Latencia de notificación < 1s desde completion
- **SSE:** Clientes reciben primer evento < 500ms desde cambio de estado
- **Batch:** Throughput > 10 documentos/minuto en ingesta masiva
- **Compresión:** Reducción de payload > 80% para embeddings

---

## Fuera de Alcance

- Cambios en workers Python de embeddings o entities (no se modifica lógica NER/embedding)
- Autenticación del API (no se agrega en este diseño)
- True batch en workers (cada documento sigue siendo un job independiente)
- Múltiples ocurrencias de entidades (solo se preserva el primer match)
