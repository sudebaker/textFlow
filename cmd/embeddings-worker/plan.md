📋 Plan de Implementación: Microservicio de Generación de Embeddings (Python)
Este documento describe el diseño completo para implementar un microservicio en Python que reciba texto y metadatos, genere embeddings por fragmentos y devuelva una estructura JSON lista para su almacenamiento en Qdrant. El servicio está diseñado para integrarse en un sistema RAG con alto tráfico.

## 🏗️ Arquitectura General

**Stack Tecnológico:**
- **Pure Python FastAPI** (más simple y consistente para ML)
- **Modelo:** BAAI/bge-m3 (multilingüe, 1024 dimensiones)
- **Integración:** Solo retorna embeddings, sin escribir en Qdrant
- **Patrón:** Stateless, thread-safe, ready for production
- **Model Storage:** `.models/` directory (excluded del git)

## 📂 Estructura de Directorios

```
cmd/embeddings-service/
├── main.py                 # FastAPI app entry point
├── requirements.txt        # Python dependencies
├── Dockerfile             # Multi-stage container build
├── README.md              # Service documentation
├── .env.example           # Environment variables template
├── app/
│   ├── __init__.py
│   ├── config/
│   │   ├── __init__.py
│   │   └── settings.py    # Configuration with Pydantic
│   ├── models/
│   │   ├── __init__.py
│   │   ├── requests.py    # Pydantic request models
│   │   └── responses.py   # Pydantic response models
│   ├── services/
│   │   ├── __init__.py
│   │   ├── embeddings.py  # Embedding generation service
│   │   └── chunking.py    # Text chunking service
│   └── api/
│       ├── __init__.py
│       └── routes/
│           ├── __init__.py
│           └── embeddings.py  # API endpoints
├── tests/
│   ├── __init__.py
│   ├── test_chunking.py
│   ├── test_embeddings.py
│   └── test_api.py
└── scripts/
    └── download_embeddings_model.py   # Model download script
```

## ✅ Fase 1: Configuración y Modelos de Datos

### Configuración (app/config/settings.py)
- MODEL_NAME: "BAAI/bge-m3"
- CHUNK_SIZE: 512 (default)
- CHUNK_OVERLAP: 64 (default)
- MAX_TEXT_SIZE: 1MB
- EMBEDDING_DIM: 1024
- BATCH_SIZE: 32
- TIMEOUT: 30s

### Request Model
```python
class EmbeddingRequest(BaseModel):
    text: str = Field(..., max_length=1048576)  # 1MB limit
    collection_name: str = Field(..., regex="^[a-zA-Z0-9_-]+$")
    doc_id: Optional[str] = None
    chunk_size: int = Field(default=512, gt=0)
    chunk_overlap: int = Field(default=64, ge=0)
    metadata: Optional[Dict[str, Any]] = None
```

### Response Model
```python
class EmbeddingResponse(BaseModel):
    collection_name: str
    doc_id: Optional[str]
    chunks: List[TextChunk]
    processing_time_ms: int
    embedding_dimension: int
    
class TextChunk(BaseModel):
    text_chunk: str
    embedding: List[float]
    metadata: Dict[str, Any]  # Incluye chunk_index y metadatos heredados
```

## ✅ Fase 2: Modelo de Embeddings y Descarga

### Modelo Seleccionado: BAAI/bge-m3
- **Dimensiones:** 1024
- **Soporte:** 100+ idiomas
- **Rendimiento:** Alta calidad multilingüe
- **Storage:** `.models/embeddings/bge-m3_model/`

### Script de Descarga (scripts/download_embeddings_model.py)
- Descarga automática desde HuggingFace
- Verificación de archivos críticos
- Metadatos del modelo
- Creación de requirements.txt

## ✅ Fase 3: Chunking y Procesamiento

### Características del Chunking Service:
- Implementación determinista por caracteres
- Soporte para overlap configurable
- Manejo de edge cases (texto vacío, overlap >= size)
- Batch processing eficiente
- Thread-safe

### Validaciones de Negocio:
- chunk_size > chunk_overlap ≥ 0
- collection_name alfanumérico con guiones/guiones bajos
- Límite de 1MB para texto
- Embeddings consistentes (mismo texto = mismo embedding)

## ✅ Fase 4: API FastAPI

### Endpoints:
- **POST /embed** - Generación de embeddings con chunking
- **GET /health** - Health check del servicio y modelo
- **GET /metrics** - Métricas básicas (opcional)

### Características:
- Validación automática con Pydantic
- Carga única del modelo al iniciar
- Generación de embeddings en batch
- Manejo de errores HTTP apropiados
- Logging estructurado

## ✅ Fase 5: Testing

### Unit Tests:
- Función de chunking (determinismo, edge cases)
- Validación de modelos Pydantic
- Generación de embeddings (consistencia)

### Integration Tests:
- API endpoints completos
- Payloads válidos/inválidos
- Rendimiento bajo carga

## ✅ Fase 6: Despliegue Docker

### Multi-stage Dockerfile:
- Stage base: Python 3.11-slim
- Stage development: con reload
- Stage production: optimizado con preload de modelos

### Variables de Entorno:
```bash
EMBEDDINGS_MODEL_PATH=.models/embeddings/bge-m3_model
EMBEDDINGS_MODEL_NAME=BAAI/bge-m3
EMBEDDINGS_DIMENSION=1024
CHUNK_SIZE=512
CHUNK_OVERLAP=64
MAX_TEXT_SIZE=1048576
BATCH_SIZE=32
TIMEOUT=30s
LOG_LEVEL=INFO
```

## ✅ Fase 7: Docker Compose (CPU/GPU)

### CPU Version (default):
```yaml
services:
  embeddings-service:
    build: ./cmd/embeddings-service
    ports: ["8084:8000"]
    volumes: [.models/embeddings:/models:ro]
    deploy:
      resources:
        limits: {cpus: '4.0', memory: 6G}
```

### GPU Version:
```yaml
services:
  embeddings-service-gpu:
    extends: embeddings-service
    runtime: nvidia
    environment:
      - TORCH_CUDA_ARCH_LIST=6.0;6.1;7.0;7.5;8.0;8.6+PTX
      - CUDA_VISIBLE_DEVICES=0
    deploy:
      resources:
        limits: {cpus: '6.0', memory: 8G}
    profiles: [gpu]
```

## ✅ Fase 8: Monitoreo y Mantenimiento

### Logging:
- Requests entrantes (INFO)
- Tiempos de procesamiento (INFO)
- Errores y fallos (ERROR)
- Métricas de memoria y carga (DEBUG)

### Health Checks:
- Disponibilidad del modelo
- Memoria disponible
- Tiempo de respuesta del modelo

### Comandos de Uso:
```bash
# Descargar modelo
python3 scripts/download_embeddings_model.py --output-dir .models/embeddings

# CPU
docker compose -f docker-compose.yml -f docker-compose.embeddings.yml up embeddings-service

# GPU
docker compose -f docker-compose.yml -f docker-compose.embeddings.yml --profile gpu up embeddings-service-gpu
```

## ✅ Notas Adicionales

- **Model Location:** Los modelos se almacenan en `.models/embeddings/` (excluido de git)
- **Git Ignore:** Añadir `.models/` al `.gitignore` general
- **Separation of Concerns:** El servicio no escribe en Qdrant, solo retorna embeddings
- **Thread Safety:** El servicio está diseñado para ser thread-safe
- **Production Ready:** Incluye health checks, resource limits, y non-root user

## ✅ Integration con Sistema Existente

El servicio sigue el mismo patrón que `gliner-service`:
- Mismo estructura de directorios relativos
- Mismo enfoque de configuración por variables de entorno
- Mismo patrón de Docker multi-stage
- Compatibilidad con el docker-compose existente