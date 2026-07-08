# Plan: Ingestion-Ready API — Especificación Técnica

**Fecha:** 2026-07-02
**Estado:** Aprobado por el committer
**Tipo:** Especificación técnica (no implementación)

---

## Meta

Facilitar la ingestión de resultados de textFlow en bases de conocimiento (Memgraph + Qdrant) sin que el consumidor tenga que implementar entity matching, descompresión de embeddings ni polling manual.

---

## No-Objetivos (fuera de alcance)

- El pipeline de workers no se reescribe.
- No se añade autenticación ni autorización en endpoints.
- El modelo de embeddings (BGE-M3) permanece igual.
- No se implementa un connector oficial Memgraph/Qdrant; solo se facilita el formato de salida.

---

## 1. Inferencias con `entity_id_refs`

### Estado actual

En `cmd/inference-worker/worker.py:245` las inferencias salen del LLM con:

```json
{"text": "...", "confidence": 0.98, "entity_refs": ["textFlow", "Go"]}
```

En `cmd/completion-worker/worker.py:503-508` solo se añade `embedding`, nunca se resuelve a `entity_id`.

### Cambio especificado

Durante `finalize_job`, por cada inferencia se calcula `entity_id_refs`:

1. Iterar sobre `entity_refs` (strings del LLM).
2. Normalizar texto: `unidecode` → eliminación de puntuación → lowercase → trim.
3. Buscar match exacto contra `EntityMinimal.text` normalizado.
4. Si no encuentra, usar `fuzz.ratio` con threshold `settings.fuzzy_match_threshold` (default 85.0).
5. Si aún no encuentra, omitir esa referencia.
6. **Deduplicar** los IDs resueltos: cada `entity_id` aparece como mucho una vez en `entity_id_refs`.
7. Si la lista resuelta está vacía, **omitir completamente** el campo `entity_id_refs` (no `[]`).

### Estructura de salida

```json
{
  "text": "textFlow is a platform...",
  "confidence": 0.98,
  "entity_refs": ["textFlow", "Go"],
  "entity_id_refs": ["96d1c6e23149", "a1b2c3d4e5f6"]
}
```

Si `entity_refs` es `["textFlow", "textFlow"]` o contiene duplicados que resuelven al mismo ID, `entity_id_refs` contiene el ID una sola vez.

Si ningún reference se resuelve, el campo `entity_id_refs` no aparece en el JSON.

### Modelo Go a actualizar

`internal/models/job.go:83-90` (`InferenceItem`):

```go
type InferenceItem struct {
    Text       string    `json:"text"`
    Confidence float32   `json:"confidence"`
    EntityRefs []string  `json:"entity_refs,omitempty"`
    EntityID   string    `json:"entity_id,omitempty"`       // deprecated, mantenido por compatibilidad
    EntityIDs  []string  `json:"entity_id_refs,omitempty"` // NEW; nil si vacío (nunca []string{})
    Embedding  []float32 `json:"embedding,omitempty"`
}
```

**Regla nil vs empty:** Nunca asignar `[]string{}`; solo `nil` o un slice con ≥1 elemento. Esto garantiza que `omitempty` funcione correctamente y el JSON no contenga `[]`.

El campo `EntityID` singular se **mantiene por compatibilidad**; no se usa en v1.1.

---

## 2. Webhook enriquecido

### Estado actual

`cmd/completion-worker/worker.py:154-198` envía:

```json
{"job_id": "...", "status": "completed", "download_url": "...", "completed_at": "..."}
```

### Cambio especificado

Nuevo header/env `WEBHOOK_PAYLOAD_MODE=summary|minimal` (default `minimal` para retrocompatibilidad).

### Circuit breaker (MAX_WEBHOOK_ITEMS = 500)

Si `entities + inferences > 500`, el payload summary **no se envía**. En su lugar:

1. Loggear warning con job_id, conteos y threshold.
2. Hacer fallback automático a modo `minimal` (solo summary counts + download_url).

### Modo `summary` (dentro de límites):

```json
{
  "job_id": "...",
  "status": "completed",
  "completed_at": "...",
  "download_url": "...",
  "schema_version": "1.1.0",
  "summary": {
    "chunks": 8,
    "entities": 1,
    "inferences": 13
  },
  "results": {
    "entities": {"96d1c6e23149": {"label": "URL", "text": "https://...", "confidence": 1}},
    "inferences": [
      {
        "chunk_id": "chunk_000",
        "text": "...",
        "confidence": 0.98,
        "entity_id_refs": ["96d1c6e23149"]
      }
    ]
  }
}
```

### Modo `minimal` (o fallback):

```json
{
  "job_id": "...",
  "status": "completed",
  "completed_at": "...",
  "download_url": "...",
  "schema_version": "1.1.0",
  "summary": {
    "chunks": 800,
    "entities": 300,
    "inferences": 5000
  }
}
```

Reglas:

- `results` incluye entities e inferences pero **NO** chunks ni text completo.
- Si el usuario quiere todo, usa `download_url`.
- Las firmas HMAC actuales se respetan.

---

## 3. Nuevos endpoints

### 3.1 `GET /v1/documents/{id}/graph`

Devuelve estructura lista de nodos y aristas, lista para `UNWIND` en Memgraph.

```json
{
  "schema_version": "1.1.0",
  "job_id": "...",
  "nodes": [
    {"id": "doc_33e271e6", "label": "Document", "props": {...}},
    {"id": "chunk_000", "label": "Chunk", "props": {...}},
    {"id": "96d1c6e23149", "label": "Entity", "props": {...}},
    {"id": "inf_chunk_000_0", "label": "Inference", "props": {...}}
  ],
  "edges": [
    {"from": "doc_33e271e6", "to": "chunk_000", "type": "HAS_CHUNK"},
    {"from": "doc_33e271e6", "to": "96d1c6e23149", "type": "HAS_ENTITY"},
    {"from": "chunk_000", "to": "inf_chunk_000_0", "type": "HAS_INFERENCE"},
    {"from": "inf_chunk_000_0", "to": "96d1c6e23149", "type": "REFERS_TO"}
  ]
}
```

IDs:

- Document: `doc_<job_id>`
- Chunk: `<chunk_id>` (ej. `chunk_000`)
- Entity: `<entity_id>` (ej. `96d1c6e23149`)
- Inference: `inf_<chunk_id>_<index>` (ej. `inf_chunk_000_0`)

Las aristas **no incluyen propiedades** (simples `from/to/type`) para v1.1.

**Optimización:** Los slices `nodes` y `edges` se pre-allocan con capacidad calculada en base al contenido de `results`, evitando re-allocation durante los append.

### 3.2 `GET /v1/documents/{id}/vectors`

Devuelve chunks e inferencias con embeddings, listos para Qdrant.

```json
{
  "schema_version": "1.1.0",
  "job_id": "...",
  "chunks": [
    {"chunk_id": "chunk_000", "text": "...", "embedding": [...]}
  ],
  "inferences": [
    {"inference_id": "...", "chunk_id": "chunk_000", "text": "...", "embedding": [...]}
  ]
}
```

Reglas:

- Soporta `?embeddings=false` para devolver payload sin vectores.
- Soporta `?fields=chunks,inferences` para filtrar.
- Soporta paginación `?page=1&limit=100`.

### 3.3 `GET /v1/documents/{id}/entities`

Entidades planas como lista.

```json
{
  "schema_version": "1.1.0",
  "job_id": "...",
  "entities": [
    {"entity_id": "96d1c6e23149", "label": "URL", "text": "https://...", "confidence": 1}
  ]
}
```

### 3.4 `GET /v1/documents/{id}/inferences`

Inferencias planas con referencias resueltas.

```json
{
  "schema_version": "1.1.0",
  "job_id": "...",
  "inferences": [
    {
      "inference_id": "inf_chunk_000_0",
      "chunk_id": "chunk_000",
      "text": "...",
      "confidence": 0.98,
      "entity_refs": ["textFlow"],
      "entity_id_refs": ["96d1c6e23149"]
    }
  ]
}
```

---

## 4. Eventos Redis enriquecidos

### Estado actual

`pkg/events_python.py:53-59` publica:

```python
{"event_type": "job_completed", "job_id": "...", "progress": 100, "status": "completed"}
```

### Cambio especificado

Añadir `metadata` con summary y download_url:

```json
{
  "event_type": "job_completed",
  "job_id": "...",
  "progress": 100,
  "status": "completed",
  "metadata": {
    "schema_version": "1.1.0",
    "download_url": ".../v1/documents/.../download",
    "summary": {
      "chunks": 8,
      "entities": 1,
      "inferences": 13
    }
  }
}
```

---

## 5. Versionado del schema

Añadir campo `schema_version` al JSON final de resultados:

```json
{"schema_version": "1.1.0", "job_id": "...", ...}
```

### Política de versionado

- **MAJOR**: cambios que rompen consumidores (reestructuración profunda).
- **MINOR**: añadir campos opcionales (no rompe).
- **PATCH**: bugfixes de formato.

Esta entrega sube de `1.0.0` (implícito) a `1.1.0` por campos opcionales y nuevos endpoints.

---

## 6. Query params en `/download`

Añadir a `GET /v1/documents/{id}/download`:

- `?embeddings=false` — omite `embeddings` y `embedding_compressed` del output.
- `?fields=text,chunks,entities,inferences,document_metadata,text_metadata,source_classification` — filtro de campos.
- `?compression=raw` existe ya; se mantiene.

---

## 7. Helper compartido para fuzzy matching

Crear `pkg/worker_common/entity_utils.py` con la lógica de normalización y fuzzy matching usada por:

- `deduplicate_entities` en completion-worker
- `_resolve_entity_refs` en completion-worker

Esto evita duplicación del código de normalización (`unidecode` + eliminación de puntuación + lowercase + trim) y del threshold fuzzy.

### Normalización con sanitización agresiva

```python
_PUNCT_RE = re.compile(r"[^\w\s]")

def normalize_entity_text(text: str) -> str:
    if not text:
        return ""
    text = unidecode(text)
    text = _PUNCT_RE.sub("", text)  # elimina puntuación residual LLM
    return text.lower().strip()
```

---

## Dependencias técnicas

- **`rapidfuzz`** (ya en `cmd/completion-worker/requirements.txt`) para `fuzz.ratio`.
- **`unidecode`** (ya en requirements) para normalización.
- **`pkg/worker_common`** es accesible desde `cmd/completion-worker` vía `PYTHONPATH` configurado en `docker-compose.yml`.

---

## Checklist de aceptación

- [ ] `entity_id_refs` aparece en las inferencias y apunta a los IDs correctos.
- [ ] `entity_id_refs` contiene solo IDs únicos (duplicados removidos).
- [ ] La puntuación en `entity_refs` se ignora en el matching.
- [ ] Si `entity_id_refs` está vacío, el campo no aparece en el JSON.
- [ ] `GET /v1/documents/{id}/graph` usa slices pre-allocados.
- [ ] `GET /v1/documents/{id}/vectors?embeddings=false` omite los vectores.
- [ ] `GET /v1/documents/{id}/entities` devuelve lista plana.
- [ ] `GET /v1/documents/{id}/inferences` incluye `entity_id_refs` únicos.
- [ ] Webhook summary hace fallback a minimal para payload > 500 items.
- [ ] El evento `job_completed` en Redis incluye `download_url` y summary.
- [ ] El JSON final incluye `schema_version: "1.1.0"`.
- [ ] Todos los tests (Go + Python) pasan.
- [ ] Swagger actualizado.
