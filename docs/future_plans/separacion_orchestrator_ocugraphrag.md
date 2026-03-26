# Separación de Responsabilidades: ia-text-orchestrator vs OCUGraphRAG

**Fecha:** 2026-03-26  
**Propósito:** Definir qué pertenece a cada proyecto y qué hay que mover/añadir

---

## Principio rector

**ia-text-orchestrator** = infraestructura de procesamiento de texto reutilizable  
**OCUGraphRAG** = aplicación de inteligencia criminal que *consume* esa infraestructura

La regla de oro: si un worker puede ser útil para SIIT, OCUGraphRAG, o cualquier otro proyecto futuro → va en ia-text-orchestrator. Si es específico del dominio criminal y del grafo de conocimiento → va en OCUGraphRAG.

---

## IA-TEXT-ORCHESTRATOR — Lo que ya tiene (no mover)

| Worker | Función |
|--------|---------|
| `extraction-worker` | Extracción de texto de PDF/DOCX/etc (Docling/Tika) |
| `entities-worker` | NER con GLiNER (personas, orgs, fechas, importes...) |
| `regex-entity-extractor` | NER por patrones (DNI, matrículas, IBANs, IPs...) |
| `embeddings-worker` | Chunking + embeddings BAAI/bge-m3 (1024d, 100+ idiomas) |
| `metadata-worker` | Extracción de metadatos de documentos |
| `completion-worker` | Agregador de resultados del pipeline completo → webhook/Redis |
| `resource-manager` | Gestión de recursos GPU/CPU |
| `docling-server` | Servicio Docling de apoyo al extraction-worker |
| Orquestador Go | Gestión de colas RabbitMQ, eventos, routing, métricas Prometheus |

---

## IA-TEXT-ORCHESTRATOR — Lo que hay que añadir

### Nuevo worker: `inference-worker`

**Por qué aquí:** La extracción de micro-inferencias es independiente del grafo. Dado un chunk + entidades, produce hechos concretos. Cualquier proyecto que necesite "hechos extraídos de texto" puede usarlo.

**Qué hace:** Recibe `{chunk_text, entities, source_type, job_id}` vía RabbitMQ, llama al LLM configurado, devuelve lista de micro-inferencias como JSON.

**Cola:** `inferences` (nueva)  
**Output:** publica en Redis + webhook igual que el resto de workers  
**Fichero:** `cmd/inference-worker/worker.py`

```
Entrada (RabbitMQ queue: "inferences"):
{
  "job_id": "uuid",
  "chunk_id": "uuid",
  "document_id": "uuid",
  "chunk_text": "...",
  "entities": [...],         ← salida del entities-worker
  "source_type": "notariado",
  "collection_name": "caso_0785",
  "llm_url": "http://vllm:8000"  ← configurable por job
}

Salida (Redis + webhook):
{
  "job_id": "uuid",
  "chunk_id": "uuid",
  "micro_inferences": [
    {"text": "Juan Pérez firmó escritura...", "confidence": 0.85, "entities": ["Juan Pérez"]},
    ...
  ]
}
```

### Nuevo worker: `source-classifier-worker`

**Por qué aquí:** Clasificar el tipo de fuente de un documento (notarial, catastral, bancario...) es una operación genérica útil para cualquier proyecto documental.

**Qué hace:** Recibe `{filename, content_preview, metadata}`, devuelve `{source_type, source_confidence}`.

**Implementación:** Reglas (no LLM) — patrones en nombre de fichero + primeras 500 palabras. Rápido y determinista.

**Cola:** `source_classification` (nueva)  
**Fichero:** `cmd/source-classifier-worker/worker.py`

### Nuevo worker: `translation-worker` (opcional, baja prioridad)

**Por qué aquí:** Traducción de documentos es 100% reutilizable entre proyectos.

**Qué hace:** Recibe `{text, source_lang, target_lang}`, devuelve texto traducido.  
**Modelo sugerido:** Helsinki-NLP/opus-mt-mul-es o similar, vía vLLM/Ollama.  
**Cola:** `translation` (nueva)

### Ampliar `completion-worker`

El completion-worker actual agrega `extraction + embeddings + entities + metadata`. Ampliar `default_required_steps` para incluir opcionalmente `inferences` y `source_classification` cuando el job los solicite:

```python
# completion-worker/worker.py
self.full_pipeline_steps = {
    "extraction", "embeddings", "entities", "metadata",
    "inferences",          # NUEVO - opcional
    "source_classification"  # NUEVO - opcional
}
```

### Ampliar el orquestador Go

Añadir rutas de pipeline para los nuevos workers:
- Pipeline estándar (existente): extraction → entities + embeddings + metadata → completion
- Pipeline con inferencias (nuevo): extraction → entities → **inferences** + **source_classification** + embeddings + metadata → completion

---

## OCUGraphRAG — Lo que se queda (dominio criminal)

| Componente | Función | Por qué aquí |
|-----------|---------|--------------|
| `AutonomousHypothesisInvestigator` | Genera y verifica hipótesis sobre un caso | Específico del dominio criminal |
| `HypothesisService` | Detecta patrones sospechosos en el grafo | Específico del grafo |
| `IntelligenceService` | Genera informes de inteligencia | Específico del dominio |
| `ContradictionService` | Detecta contradicciones entre evidencias | Específico del dominio |
| `NetworkService` | Análisis de red de actores | Específico del dominio |
| `GraphService` / `VectorService` | CRUD sobre Memgraph + Qdrant | Infraestructura del proyecto |
| `EntityResolutionService` | Deduplicación de entidades en el grafo | Específico del grafo |
| ACH (Análisis de Hipótesis Competitivas) | Panel de hipótesis rivales | Específico del dominio |
| RBAC por colecciones | Control de acceso por caso/investigación | Específico del dominio |

### Lo que OCUGraphRAG añade (nuevo, pero no va al orquestador)

**`EpisodeClusteringService`** — Agrupa micro-inferencias en episodios  
**Por qué aquí y no en el orquestador:** El clustering usa el grafo Memgraph (proximidad 1-2 hops) y los embeddings de Qdrant. Depende de la infraestructura de almacenamiento del propio proyecto. No es transferible sin esas dependencias.

**`InferenceSynthesisService`** — Genera meso y macro inferencias  
**Por qué aquí:** Opera sobre los episodios ya almacenados en Memgraph. Es lógica de negocio del dominio.

**Informes en dos velocidades** (QuickReport + DeepReport)  
**Por qué aquí:** Son informes sobre el grafo criminal. El contenido es del dominio.

---

## Flujo de datos entre los dos proyectos

```
USUARIO
  │
  ▼
OCUGraphRAG (FastAPI)
  │  POST /ingest/upload (documento)
  │
  ▼
ia-text-orchestrator (Go orchestrator)
  │  Publica job en RabbitMQ
  │
  ├──► extraction-worker    → texto extraído
  ├──► source-classifier-worker → {source_type, confidence}  [NUEVO]
  │
  ├──► entities-worker      → entidades NER
  ├──► regex-extractor      → entidades por patrón
  │
  ├──► inference-worker     → micro-inferencias  [NUEVO]
  ├──► embeddings-worker    → chunks + vectores
  ├──► metadata-worker      → metadatos
  │
  └──► completion-worker    → agrega todo, publica webhook
         │
         ▼
    OCUGraphRAG webhook receiver
         │
         ├── Guarda chunks/vectores → Qdrant
         ├── Guarda entidades/relaciones → Memgraph
         ├── Guarda micro-inferencias → Memgraph + Qdrant  [NUEVO]
         │
         ▼
    EpisodeClusteringService (Celery task)  [NUEVO en OCUGraphRAG]
         │
         ▼
    InferenceSynthesisService (Celery task)  [NUEVO en OCUGraphRAG]
         │
         ▼
    AutonomousHypothesisInvestigator (ya existe, recibe input enriquecido)
```

---

## Resumen de trabajo por proyecto

### ia-text-orchestrator (3 workers nuevos)

| Tarea | Esfuerzo |
|-------|---------|
| `inference-worker` nuevo | Medio |
| `source-classifier-worker` nuevo | Bajo |
| `translation-worker` nuevo | Bajo (si hay GPU) |
| Ampliar `completion-worker` con nuevos steps | Bajo |
| Añadir rutas de pipeline al orquestador Go | Medio |

### OCUGraphRAG (consume + añade lógica de dominio)

| Tarea | Esfuerzo |
|-------|---------|
| Webhook receiver para micro-inferencias y source_type | Bajo |
| Schema Memgraph: nodos MicroInference, Episode, Meso, Macro | Bajo |
| `EpisodeClusteringService` (Louvain, 3 canales) | Medio |
| `InferenceSynthesisService` (meso + macro) | Medio |
| Alimentar `AutonomousHypothesisInvestigator` con MacroInferences | Bajo |
| Informes en dos velocidades | Medio |
| Citaciones enriquecidas con source_type | Bajo |

---

## Sobre el nombre "ia-text-orchestrator"

Técnicamente hace más que orquestar texto: gestiona un pipeline completo de procesamiento documental con extracción, NER, embeddings e inferencias. Nombres alternativos más descriptivos:

- `docpipeline` — directo, describe el propósito
- `textflow` — evoca el flujo de procesamiento
- `docworkers` — explícito sobre la arquitectura
- `ingestpipe` — enfocado en ingesta
- **`docflow`** — equilibrio entre descripción y brevedad ← mi favorito

Pero el nombre es lo de menos, el diseño es lo que importa 💋
