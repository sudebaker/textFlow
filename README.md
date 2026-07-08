# textFlow

**Event-driven microservices platform for intelligent document processing.**

textFlow extrae, analiza y enriquece contenido de documentos, imágenes y archivos de audio usando orquestación en Go y workers Python con modelos ML. Diseñado para despliegue **air-gapped** (sin acceso a internet).

---

## 🚀 Quick Start

```bash
# 1. Clonar y configurar
git clone https://github.com/anomalyco/textflow.git
cd textflow
cp .env.example .env

# 2. Descargar modelos ML (~3.6 GB, primera vez)
make setup-models

# 3. Iniciar infraestructura (RabbitMQ, Redis, Docling)
make infra-up

# 4. Iniciar todos los servicios
make docker-up

# 5. Verificar salud
curl http://localhost:9080/health
```

---

## 📋 Arquitectura

```
                                    ┌─────────────────────┐
                                    │     RabbitMQ         │
Document ──▶ [Orchestrator] ───────▶ │  extract_text queue  │
           (Go/Gin, port 8080)      └──────────┬──────────┘
                                    ┌──────────┴──────────┐
                                    ▼                     ▼
                         ┌──────────────────┐  ┌─────────────────┐
                         │  extraction-worker │  │  audio-worker   │
                         │  (Docling)         │  │  (Whisper)      │
                         └────────┬─────────┘  └─────────────────┘
                                  │ texto extraído
                    ┌─────────────┼─────────────┐
                    ▼             ▼             ▼
           ┌────────────┐ ┌───────────┐ ┌──────────┐
           │ embeddings │ │  entities │ │ metadata │
           │  -worker   │ │  -worker  │ │  -worker  │
           │ bge-m3     │ │  GLiNER   │ │           │
           └────────────┘ └───────────┘ └───────────┘
                    │             │             │
                    └─────────────┼─────────────┘
                                  ▼
                         ┌──────────────────┐
                         │ completion-worker│
                         │ (agregador)      │
                         └────────┬─────────┘
                                  ▼
                              [Redis]
                                  │
                              resultados
```

### Servicios

| Servicio | Lenguaje | Puerto | Propósito | GPU |
|----------|----------|--------|-----------|-----|
| `orchestrator` | Go/Gin | 8080 | REST API, orquestación, SSRF validation | No |
| `resource-manager` | Go | 9090 | Monitoreo de memoria GPU | No |
| `extraction-worker` | Python | — | Extracción Docling (PDF, DOCX, PPTX) | Opcional |
| `embeddings-worker` | Python | — | Embeddings BAAI/bge-m3 (1024 dims) | Opcional |
| `entities-worker` | Python | — | NER con GLiNER + Regex patterns | Opcional |
| `metadata-worker` | Python | — | Analytics de texto | No |
| `inference-worker` | Python | — | Micro-inferencias con vLLM | Sí |
| `completion-worker` | Python | — | Agregación de resultados, webhooks | No |
| `audio-worker` | Python | — | Transcripción Whisper | Opcional |
| `image-worker` | Python | — | Análisis multimodal LLM | Sí |
| `regex-entity-extractor` | Python | 8081 | Extracción PII (email, teléfono, IBAN...) | No |

### Infraestructura

| Servicio | Imagen | Puertos | Propósito |
|----------|--------|---------|-----------|
| RabbitMQ | rabbitmq:3.13 | 5672, 15692 | Message broker |
| Redis | redis:7-alpine | 6379 | Estado (TTL 24h) |
| Docling | quay.io/docling-project/docling-serve | 5001 | Extracción de documentos |
| Redis Exporter | oliver006/redis_exporter | 9121 | Métricas Prometheus |

---

## ✨ Características

- **Procesamiento multimodal:** Documentos (PDF, DOCX, PPTX), imágenes, audio, spreadsheets
- **Embeddings:** 1024 dimensiones con BAAI/bge-m3
- **Reconocimiento de entidades:** GLiNER (PERSON, ORGANIZATION, LOCATION, MONEY) + 20+ patterns regex (EMAIL, PHONE, IBAN, DNI, etc.)
- **Transcripción de audio:** Whisper con speaker diarization opcional
- **Análisis de imágenes:** LLM multimodal para descripción y extracción
- **API REST:** Upload, polling con SSE streaming, batch processing
- **Webhook notifications:** Notificaciones al completar
- **Air-gapped:** 100% offline tras descarga inicial de modelos
- **Métricas Prometheus:** jobs_total, job_duration_seconds, queue depths

---

## 📂 Estructura del Proyecto

```
textflow/
├── cmd/                          # Servicios
│   ├── orchestrator/             # API REST Go (puerto 8080)
│   ├── resource-manager/         # Monitoreo GPU Go (puerto 9090)
│   ├── extraction-worker/        # Python - Docling
│   ├── embeddings-worker/        # Python - BAAI/bge-m3
│   ├── entities-worker/          # Python - GLiNER NER
│   ├── metadata-worker/          # Python - Analytics
│   ├── inference-worker/         # Python - vLLM (opcional)
│   ├── completion-worker/        # Python - Agregador
│   ├── audio-worker/             # Python - Whisper
│   ├── image-worker/             # Python - Multimodal LLM
│   └── regex-entity-extractor/   # Python - PII patterns
├── internal/                      # Paquetes Go compartidos
│   ├── broker/                   # Cliente RabbitMQ
│   ├── config/                   # Configuración
│   ├── events/                   # Event bus (Redis Pub/Sub)
│   ├── health/                   # Health checking
│   ├── middleware/              # Rate limiting, circuit breaker
│   ├── models/                  # Modelos de datos
│   └── redis/                   # Cliente Redis
├── pkg/                          # Paquetes compartidos
│   ├── logging/                  # Logging estructurado Go
│   ├── metrics/                  # Prometheus metrics Go
│   └── worker_common/           # BaseWorker Python
├── deploy/
│   ├── docker/                   # Docker Compose
│   │   ├── docker-compose.yml    # Servicios completos
│   │   └── docker-compose.gpu.yml # Con GPU
│   └── package/                  # Scripts air-gapped
├── docs/                         # Documentación
│   ├── API.md                    # Referencia completa de la API
│   ├── AIRGAPPED_DEPLOYMENT.md  # Guía deployment offline
│   └── swagger/                  # OpenAPI specs
├── tools/
│   └── client/                   # CLI client en Go
├── models/                       # Modelos ML (~3.6 GB, gitignored)
├── bin/                          # Binarios compilados (gitignored)
├── Makefile                      # Comandos de build
└── README.md                     # Este archivo
```

---

## 🔧 Uso de la API

### Subir y procesar un documento

```bash
curl -X POST http://localhost:9080/v1/documents/upload \
  -F "file=@documento.pdf" \
  -F "features=inferences"
```

### Verificar estado del job

```bash
curl http://localhost:9080/v1/documents/{job_id}
# Respuesta: {"job_id":"abc-123","status":"completed","current_step":"metadata"}
```

### Descargar resultados

```bash
curl http://localhost:9080/v1/documents/{job_id}/download | gunzip > resultados.json
```

### Streaming con SSE

```bash
curl -N http://localhost:9080/v1/jobs/{job_id}/stream
```

### Batch processing

```bash
curl -X POST http://localhost:9080/v1/documents/batch \
  -H "Content-Type: application/json" \
  -d '{"documents":[{"text":"..."},{"text":"..."}]}'
```

### Endpoints principales

| Método | Endpoint | Descripción |
|--------|----------|-------------|
| `GET` | `/health` | Health check con estado de Redis/RabbitMQ |
| `POST` | `/v1/documents/process` | Crear job (base64 o URL) |
| `POST` | `/v1/documents/upload` | Subir archivo (multipart/form-data) |
| `GET` | `/v1/documents/{job_id}` | Estado del job (polling) |
| `GET` | `/v1/documents/{job_id}/download` | Descargar resultados (gzip JSON) |
| `DELETE` | `/v1/documents/{job_id}` | Eliminar job |
| `GET` | `/v1/jobs/{job_id}/stream` | Eventos SSE |
| `POST` | `/v1/documents/batch` | Batch de documentos |
| `GET` | `/v1/batches/{batch_id}/status` | Estado del batch |
| `GET` | `/metrics` | Métricas Prometheus |

---

## 🛠️ Desarrollo

### Comandos Makefile

```bash
make help                  # Mostrar todos los comandos

# Ejecutar localmente (requiere infra primero)
make run-orchestrator       # Orchestrator en puerto 8080
make run-resource           # Resource manager en puerto 9090
make run-embeddings-worker  # Worker de embeddings
make run-entities-worker    # Worker de entidades
make run-audio-worker       # Worker de audio
make run-image-worker       # Worker de imágenes
make run-workers            # Todos los workers

# Infraestructura
make infra-up              # Iniciar RabbitMQ, Redis, Docling
make infra-down            # Detener infraestructura

# Docker
make docker-up              # Iniciar todos los servicios
make docker-down            # Detener todos
make docker-logs            # Ver logs de todos los servicios

# Testing
make test                   # Tests Go
make test-coverage          # Tests con coverage HTML
make test-python            # Tests Python (pytest)

# Calidad
make lint                   # Linter Go (golangci-lint)
make lint-fix               # Corregirissues del linter
make format                 # Formatear Go y Python

# Build
make build                  # Compilar todos los binarios → bin/
make build-orchestrator     # Solo orchestrator
make build-resource-manager # Solo resource-manager
make build-client          # Solo CLI client
```

### Tests unitarios

```bash
# Go
go test -v ./internal/redis/...
go test -v ./internal/broker/...

# Python
pytest cmd/embeddings-worker/tests/ -v
pytest cmd/entities-worker/tests/ -v
pytest cmd/*/tests -v
```

---

## 📦 Despliegue Air-Gapped

Diseñado para funcionar **sin acceso a internet** tras la descarga inicial de modelos.

### 1. Preparar bundle de deployment

```bash
make package   # ~43 GB bundle completo en dist/
```

### 2. Transferir al servidor objetivo

```bash
make deploy HOST=10.0.0.5   # Requiere rsync
```

### 3. Instalar en target

```bash
make install-remote HOST=10.0.0.5
ssh 10.0.0.5 "bash ~/textflow-deployment/install.sh"
```

### Modelos requeridos (~3.6 GB)

```
models/
├── bge-m3/                    # Embeddings (~1 GB)
├── gliner-small-v2.1/         # NER (~800 MB)
├── deberta-v3-small/          # Tokenizer backbone (~300 MB)
├── modern-gliner/             # GLiNER variante (~1.5 GB)
└── docling/                   # Artefactos Docling
```

---

## 🔌 Ingestion-Ready Output

textFlow outputs results in formats ready for direct ingestion into **Memgraph** (graph) and **Qdrant** (vectors). All endpoints return `schema_version: "1.1.0"`.

### Graph endpoint — Memgraph

```
GET /v1/documents/{id}/graph
```

Returns nodes and edges as a flat list for `UNWIND` import:

```json
{
  "schema_version": "1.1.0",
  "job_id": "abc123",
  "nodes": [
    {"id": "doc_abc123", "label": "Document", "props": {"title": "README"}},
    {"id": "chunk_000", "label": "Chunk", "props": {"start_offset": 0}},
    {"id": "96d1c6e23149", "label": "Entity", "props": {"label": "ORG", "text": "textFlow"}},
    {"id": "inf_chunk_000_0", "label": "Inference", "props": {"text": "textFlow is fast", "confidence": 0.98}}
  ],
  "edges": [
    {"from": "doc_abc123", "to": "chunk_000", "type": "HAS_CHUNK"},
    {"from": "doc_abc123", "to": "96d1c6e23149", "type": "HAS_ENTITY"},
    {"from": "chunk_000", "to": "inf_chunk_000_0", "type": "HAS_INFERENCE"},
    {"from": "inf_chunk_000_0", "to": "96d1c6e23149", "type": "REFERS_TO"}
  ]
}
```

### Vectors endpoint — Qdrant

```
GET /v1/documents/{id}/vectors?embeddings=false&fields=chunks,inferences&page=1&limit=100
```

Returns chunks and inferences with optional embeddings for Qdrant point upload.

### Entities and Inferences endpoints

```
GET /v1/documents/{id}/entities   # flat entity list
GET /v1/documents/{id}/inferences  # flat inference list with resolved entity_id_refs
```

Inferences include `entity_id_refs` — entity IDs resolved from LLM text references (`entity_refs`) via fuzzy matching, ready for linking in your knowledge graph.

### Webhook enrichment

Configure `WEBHOOK_PAYLOAD_MODE=summary` to receive entities and inferences directly in the webhook POST, with automatic fallback to minimal mode when payload exceeds 500 items.

### Schema version

All v1.1.0 outputs include `schema_version`. The `entity_id_refs` field on inferences uses a 0-1 fuzzy threshold (default 0.85).

---

## 📊 Monitoreo

### Health endpoint

```bash
curl http://localhost:9080/health
# {
#   "status": "up",
#   "components": {
#     "redis": "up",
#     "rabbitmq": "up"
#   }
# }
```

### Métricas Prometheus

```bash
curl http://localhost:9080/metrics
```

Métricas disponibles:

- `textflow_jobs_total{status,type}` — Jobs totales por estado
- `textflow_job_duration_seconds` — Histograma de duración
- `textflow_jobs_in_progress` — Jobs procesando actualmente
- `textflow_queue_depth` — Profundidad de colas RabbitMQ
- `textflow_http_requests_total{method,path,status}` — Requests HTTP

---

## 🔒 Seguridad

- **Air-gapped:** Sin acceso a internet en producción
- **SSRF protection:** Validación de URLs, bloquea metadata clouds
- **Validación de archivos:** Whitelist de extensiones, límites de tamaño
- **Rate limiting:** 100 req/s por IP (configurable)
- **Circuit breaker:** Protección contra fallos en cascada
- **Usuarios no-root:** Containers corren como usuario no privilegiado

---

## 🧪 Testing Offline (Entities Worker)

```bash
# Diagnosticar problemas de modo offline
python cmd/entities-worker/offline_diagnosis.py

# Test de extracción sin red
docker run --network=none textflow-entities-worker python worker.py

# Verificación de modelos
ls -la models/gliner-small-v2.1/
# Debe incluir: config.json, pytorch_model.bin, spm.model, tokenizer_config.json
```

---

## 📚 Documentación Adicional

| Archivo | Descripción |
|---------|-------------|
| `docs/API.md` | Referencia completa de la API REST |
| `docs/AIRGAPPED_DEPLOYMENT.md` | Guía detallada de deployment offline |
| `cmd/entities-worker/README.md` | Documentación del worker de entidades |
| `AGENTS.md` | Convenciones de código y arquitectura |

---

## 📄 Licencia

MIT
