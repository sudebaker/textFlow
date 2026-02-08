# Roadmap - IA Text Orchestrator ETL Pipeline

**Fecha**: 2026-02-07
**Versión**: 2.0
**Arquitecto**: Senior AI Agent

---

## 🎯 Visión del Proyecto

Somos un **servicio ETL especializado en documentos**: recibe PDF/documentos → extrae texto, metadatos, chunks, embeddings y entidades → devuelve JSON completo vía Redis al cliente.

**No somos una base de datos vectorial**. Solo procesamos y devolvemos resultados procesados.

---

## 🏗️ Arquitectura Final

```
┌─────────────────────────────────────────────────────────────┐
│                    ORCHESTRATOR (Go)                         │
│  POST /v1/documents/process → RabbitMQ message              │
│  GET /v1/documents/{id} → Redis GET results                 │
└──────────────────────┬──────────────────────────────────────┘
                       │
        ┌──────────────┼──────────────┬──────────────┐
        ▼              ▼              ▼              ▼
   ┌─────────────┬──────────────┬──────────────┬──────────────┐
   │ Extraction  │  Embeddings  │  Entities    │  Completion  │
   │  Worker     │   Worker     │   Worker     │   Worker     │
   └──────┬──────┴──────┬───────┴──────┬───────┴──────┬───────┘
          │             │              │              │
          ▼             ▼              ▼              ▼
    ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────┐
    │unstructured│  │bge-m3   │  │GLiNER    │  │Aggregate all │
    │+ exiftool  │  │(chunks) │  │(config)  │  │Generate JSON │
    └──────┬─────┘  └────┬─────┘  └────┬────┘  └──────┬───────┘
           │             │             │              │
           │ Chunks      │ Embeddings  │ Entities     │
           │ + Metadata  │ (dict)      │ + chunks     │
           └─────────────┴─────────────┴──────────────┘
                         │
                         ▼
                    ┌────────────┐
                    │   Redis    │
                    │ job:results│
                    └────────────┘
```

---

## 🔄 Flujo del Pipeline

### **Orchestrator**
1. Recibe POST con `document_url` o `document_base64`
2. Genera `job_id` (UUID o timestamp)
3. Publica mensaje a RabbitMQ con `{job_id, document_url, entity_types}`
4. Devuelve `{job_id, status: "processing"}`

### **Extraction Worker**
1. Descarga/lee documento desde URL o base64
2. **Unstructured API** → extrae texto + metadatos del PDF
3. **exiftool** → extrae metadatos técnicos adicionales
4. **Chunking local** → divide texto en chunks de 512 tokens (50 overlap)
5. Guarda en Redis:
   - `orchestrator:job:{id}:text` (texto completo)
   - `orchestrator:job:{id}:chunks` (lista de chunks)
   - `orchestrator:job:{id}:metadata:document` (metadatos PDF)
6. Publica a RabbitMQ: `{job_id, chunks}` a queues de embeddings, entities, metadata

### **Embeddings Worker** (paralelo)
1. Recibe `{job_id, chunks}`
2. Para cada chunk → BAAI/bge-m3 → embedding (1024 dims)
3. Resultado: `{chunk_000: [floats], chunk_001: [floats], ...}`
4. Guarda en Redis: `orchestrator:job:{id}:embeddings`
5. Publica evento: step_completed

### **Entities Worker** (paralelo)
1. Recibe `{job_id, chunks, entity_types}`
2. Para cada chunk → GLiNER.predict_entities(chunk, entity_types)
3. Mapea cada entidad a su `chunk_id`
4. Resultado: `[{text, label, confidence, chunk_id}, ...]`
5. Guarda en Redis: `orchestrator:job:{id}:entities`
6. Publica evento: step_completed

### **Metadata Worker** (paralelo)
1. Recibe `{job_id, text}`
2. Analiza texto:
   - Idioma (lenguaje detection)
   - Conteo de palabras/líneas/caracteres
   - Readability score
   - Detección de URLs/emails/números
3. Guarda en Redis: `orchestrator:job:{id}:metadata:text`
4. Publica evento: step_completed

### **Completion Worker**
1. Escucha Pub/Sub: job_progress events
2. Espera a que todos los steps estén completados
3. Agrega desde Redis:
   - text
   - chunks
   - embeddings (dict)
   - entities
   - metadata (document + text)
4. Genera JSON final estructurado
5. Guarda en Redis: `orchestrator:job:{id}:results`
6. Actualiza status: "completed"
7. Publica evento: job_completed

### **Cliente**
```
GET /v1/documents/{job_id}
→ Devuelve JSON completo desde Redis
```

---

## 📋 Variables de Entorno

```bash
# Chunking
CHUNK_SIZE_TOKENS=512          # Tamaño de cada chunk en tokens
CHUNK_OVERLAP_TOKENS=50         # Solapamiento entre chunks

# Entity Extraction
ENTITY_TYPES=PER,ORG,LOC,DATE,MONEY  # Tipos de entidades a extraer

# Unstructured API
UNSTRUCTURED_URL=http://unstructured:8000

# Redis
REDIS_URL=redis://redis:6379

# RabbitMQ
RABBITMQ_URL=amqp://rabbitmq:5672/

# Metadata extraction
EXIFTOOL_PATH=/usr/bin/exiftool
```

---

## 📦 Estructura del JSON de Salida

```json
{
  "job_id": "1770491046097136204",
  "status": "completed",
  "created_at": "2026-02-07T19:11:00Z",
  "completed_at": "2026-02-07T19:14:30Z",

  "document_metadata": {
    "filename": "sentencia.pdf",
    "author": "Tribunal Supremo",
    "title": "Sentencia 1000/2025",
    "creation_date": "2025-12-09T10:30:00Z",
    "modification_date": "2025-12-09T10:30:00Z",
    "pages": 238,
    "file_size_bytes": 2008867,
    "sha256": "4737b8c42b5e88f1e4a0383e47ff31efcc85f8ae725be64f459a1cfa8fb598d2",
    "producer": "PDF Producer Name",
    "encrypted": false,
    "exif_data": {
      "Software": "...",
      "Make": "...",
      "Model": "..."
    }
  },

  "text_metadata": {
    "language": "es",
    "char_count": 547145,
    "word_count": 109774,
    "line_count": 10122,
    "avg_sentence_length": 28.4,
    "readability_score": 45.2,
    "has_urls": false,
    "has_emails": true,
    "has_numbers": true,
    "encoding": "utf-8"
  },

  "text": "...(texto completo del documento)...",

  "chunks": [
    {
      "chunk_id": "chunk_000",
      "text": "...(primeros 512 tokens del texto)...",

      "start_offset": 0,
      "end_offset": 2100
    },
    {
      "chunk_id": "chunk_001",
      "text": "...(siguientes 512 tokens, con overlap de 50 tokens)...",

      "start_offset": 2050,
      "end_offset": 4150
    }
  ],

  "embeddings": {
    "model": "BAAI/bge-m3",
    "dimension": 1024,
    "chunk_000": [-0.06539417058229446, 0.026472052559256554, ...],
    "chunk_001": [0.012345678901234567, -0.04567890123456789, ...]
  },

  "entities": [
    {
      "entity_id": "ent_001",
      "text": "Tribunal Supremo",
      "label": "ORG",
      "confidence": 0.98,
      "chunk_id": "chunk_005",
      "position_in_chunk": 12
    },
    {
      "entity_id": "ent_002",
      "text": "Andrés Martínez Arrieta",
      "label": "PER",
      "confidence": 0.95,
      "chunk_id": "chunk_010",
      "position_in_chunk": 34
    }
  ]
}
```

---

## ✅ Checklist de Implementación

### **Fase 1: Extraction Worker (Refactor)**
- [ ] 1.1 Agregar `exiftool` al Dockerfile
- [ ] 1.2 Crear función `extract_pdf_metadata(filepath)` → dict
- [ ] 1.3 Crear función `chunk_text(text, chunk_size, overlap)` → list[dict]
- [ ] 1.4 Usar `tiktoken` para contar tokens correctamente
- [ ] 1.5 Guardar chunks en Redis
- [ ] 1.6 Pasar `chunks` en el mensaje RabbitMQ (no `text`)

### **Fase 2: Embeddings Worker (Refactor)**
- [ ] 2.1 Cambiar input: recibir `chunks` (no `text` plano)
- [ ] 2.2 Loop: para cada chunk → embedding
- [ ] 2.3 Output: dict `{chunk_id: [floats], ...}` (NO lista simple)
- [ ] 2.4 Guardar embeddings en Redis como JSON

### **Fase 3: Entities Worker (Refactor)**
- [ ] 3.1 Leer `entity_types` del RabbitMQ message
- [ ] 3.2 Usar: `GLiNER.predict_entities(chunk_text, entity_types)`
- [ ] 3.3 Mapear: entity → chunk_id
- [ ] 3.4 Output: lista de entities con chunk_id

### **Fase 4: Completion Worker (Refactor)**
- [ ] 4.1 Esperar ALL steps completados
- [ ] 4.2 Agregar: document_metadata (Unstructured + exiftool)
- [ ] 4.3 Agregar: text_metadata (idioma, counts, etc.)
- [ ] 4.4 Agregar: chunks, embeddings (dict), entities
- [ ] 4.5 Generar JSON final estructurado
- [ ] 4.6 Guardar en Redis: `orchestrator:job:{id}:results`

### **Fase 5: Orchestrator API (Nuevos endpoints)**
- [ ] 5.1 POST `/v1/documents/process` acepta `entity_types` opcional
- [ ] 5.2 GET `/v1/documents/{id}` devuelve JSON desde Redis

### **Fase 6: Variables de Entorno**
- [ ] 6.1 Agregar al docker-compose y Dockerfiles
- [ ] 6.2 Documentar en README.md

### **Fase 7: Testing**
- [ ] 7.1 Test end-to-end con documento PDF real
- [ ] 7.2 Verificar que chunks tienen overlap correcto
- [ ] 7.3 Verificar embeddings por chunk
- [ ] 7.4 Verificar entidades mapeadas a chunks

---

## 🛠️ Dependencias Nuevas

```txt
# requirements.txt (Python workers)
# ... existing dependencies ...

# Nuevas:
tiktoken>=0.5.0          # Para conteo preciso de tokens
pyexiftool>=0.5.0        # Wrapper de exiftool (o usar subprocess)
python-magic>=0.4.27     # Detección de tipos MIME
langdetect>=1.0.9       # Detección de idioma (55+ lenguajes)
textstat>=21.11.0        # Readability scores (Flesch, etc.)
```

```dockerfile
# Install in extraction worker
RUN apt-get update && apt-get install -y --no-install-recommends \
    exiftool \
    && rm -rf /var/lib/apt/lists/*
```

---

## 📊 Métricas del Pipeline

| Métrica | Valor Esperado |
|---------|----------------|
| Tiempo extracción PDF (1MB) | < 10s |
| Tiempo chunking (100K tokens) | < 2s |
| Tiempo embeddings (100 chunks) | < 30s |
| Tiempo entities (100 chunks) | < 15s |
| Tiempo metadata análisis | < 1s |
| **Total pipeline (100K tokens)** | **< 60s** |
| Tamaño JSON salida (100K tokens) | ~2-3 MB |
| Embeddings: 1024 dims × chunks | ~4KB por chunk |

---

## 🚨 Problemas Resueltos (de la versión 1.0)

| Problema | Solución |
|----------|----------|
| Embeddings solo 1 vector total | Chunking → embedding por chunk |
| Entidades no detectadas | GLiNER con tipos configurables |
| Metadatos PDF incompletos | exiftool para metadatos técnicos |
| JSON sin estructura clara | Estructura definida con document_metadata, text_metadata, chunks, embeddings, entities |
| Sin overlap en chunks | 50 tokens overlap para no perder entidades en límites |

---

## 📅 Timeline Estimado

| Fase | Duración | Estado |
|------|----------|--------|
| Fase 1: Extraction Worker | 2 días | ⏳ Pendiente |
| Fase 2: Embeddings Worker | 1 día | ⏳ Pendiente |
| Fase 3: Entities Worker | 1 día | ⏳ Pendiente |
| Fase 4: Completion Worker | 1 día | ⏳ Pendiente |
| Fase 5: Orchestrator API | 0.5 días | ⏳ Pendiente |
| Fase 6: Variables de entorno | 0.5 días | ⏳ Pendiente |
| Fase 7: Testing E2E | 1 día | ⏳ Pendiente |
| **Total** | **~8 días** | - |

---

## 🔗 Referencias

- [BAAI/bge-m3](https://huggingface.co/BAAI/bge-m3) - Modelo de embeddings
- [GLiNER](https://github.com/urchade/GLiNER) - Entity recognition
- [Unstructured.io](https://unstructured-io.github.io/unstructured/) - Text extraction
- [TikToken](https://github.com/openai/tiktoken) - Token counting
- [ExifTool](https://exiftool.org/) - Metadata extraction
- [tiktokenizer](https://github.com/transitive-bullshit/tiktokenizer) - Token visualization

---

**Documento generado**: 2026-02-07
**Próxima actualización**: Tras completar cada fase