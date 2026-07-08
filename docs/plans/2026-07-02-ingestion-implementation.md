# Plan: Ingestion-Ready API — Implementación

**Fecha:** 2026-07-02
**Estado:** Aprobado por el committer
**Fases:** A (completion-worker) → B (orchestrator Go) → C (webhook) → D (eventos Redis) → E (docs) → F (e2e)

---

## Resumen de arquitectura

Los cambios se concentran en:
- **completion-worker** (entity_id_refs, schema_version, webhook mode, eventos enriquecidos)
- **orchestrator Go** (nuevos endpoints graph/vectors/entities/inferences)
- **pkg/events_python.py** (eventos enriquecidos)
- **pkg/worker_common/entity_utils.py** (nuevo, helper fuzzy matching)

El pipeline de extracción/embeddings/inference **no se toca**.

---

## Fase A: `entity_id_refs` + `schema_version` en completion-worker

### Task A1 — Crear helper compartido `pkg/worker_common/entity_utils.py`

```python
# pkg/worker_common/entity_utils.py

import re
from typing import Dict, List, Set
from unidecode import unidecode
from rapidfuzz import fuzz

_PUNCT_RE = re.compile(r"[^\w\s]")

def normalize_entity_text(text: str) -> str:
    """Normaliza texto para matching: unidecode + eliminación puntuación + lowercase + strip."""
    if not text:
        return ""
    text = unidecode(text)
    text = _PUNCT_RE.sub("", text)
    return text.lower().strip()

def fuzzy_match_score(a: str, b: str) -> float:
    """Devuelve score 0-100 de similitud entre dos strings."""
    return fuzz.ratio(a, b)

def resolve_entity_refs(
    entity_refs: List[str],
    entities_dict: Dict[str, dict],
    fuzzy_threshold: float = 85.0
) -> List[str]:
    """
    Dada una lista de entity_refs (strings del LLM) y un diccionario
    de entidades deduplicadas, devuelve la lista de entity_id correspondientes.

    Estrategia:
    1. Match exacto normalizado
    2. Si no encuentra, fuzzy match con score >= threshold
    3. Si aún no encuentra, la referencia se omite
    4. IDs resultantes son únicos (Set → sorted list)
    """
    resolved: Set[str] = set()
    for ref in entity_refs:
        normalized_ref = normalize_entity_text(ref)
        if not normalized_ref:
            continue

        # Match exacto
        for ent_id, ent in entities_dict.items():
            if normalize_entity_text(ent.get("text", "")) == normalized_ref:
                resolved.add(ent_id)
                break
        else:
            # Fuzzy match
            best_score = 0.0
            best_id = None
            for ent_id, ent in entities_dict.items():
                score = fuzzy_match_score(normalized_ref, normalize_entity_text(ent.get("text", "")))
                if score >= fuzzy_threshold and score > best_score:
                    best_score = score
                    best_id = ent_id
            if best_id:
                resolved.add(best_id)
    return sorted(resolved)
```

**Step 1:** Crear el archivo.

**Step 2:** Verificar import:
```bash
cd /path/to/textflow && python -c "from pkg.worker_common.entity_utils import normalize_entity_text, resolve_entity_refs; print('OK')"
```

---

### Task A2 — Tests para `resolve_entity_refs`

Crear `cmd/completion-worker/tests/test_entity_id_refs.py`:

```python
import pytest
from pkg.worker_common.entity_utils import resolve_entity_refs

def test_resolve_exact_match():
    entities = {
        "abc123": {"label": "ORG", "text": "textFlow", "confidence": 0.9}
    }
    refs = ["textFlow"]
    resolved = resolve_entity_refs(refs, entities, fuzzy_threshold=85.0)
    assert resolved == ["abc123"]

def test_resolve_fuzzy_match():
    entities = {
        "abc123": {"label": "ORG", "text": "textFlow", "confidence": 0.9}
    }
    refs = ["TEXTFLOW"]  # uppercase, no match exacto
    resolved = resolve_entity_refs(refs, entities, fuzzy_threshold=85.0)
    assert resolved == ["abc123"]

def test_resolve_no_match_omits():
    entities = {
        "abc123": {"label": "ORG", "text": "textFlow", "confidence": 0.9}
    }
    refs = ["NonExistent"]
    resolved = resolve_entity_refs(refs, entities, fuzzy_threshold=85.0)
    assert resolved == []

def test_resolve_multiple_refs():
    entities = {
        "abc123": {"label": "ORG", "text": "textFlow", "confidence": 0.9},
        "def456": {"label": "LANG", "text": "Go", "confidence": 1.0}
    }
    refs = ["textFlow", "Go"]
    resolved = resolve_entity_refs(refs, entities, fuzzy_threshold=85.0)
    assert set(resolved) == {"abc123", "def456"}

def test_resolve_ignores_punctuation():
    """La puntuación residual del LLM se elimina antes del matching."""
    entities = {
        "abc123": {"label": "ORG", "text": "textFlow", "confidence": 0.9},
        "def456": {"label": "LANG", "text": "Go", "confidence": 1.0},
    }
    refs = ["textFlow.", "Go,", "textFlow!!!"]
    resolved = resolve_entity_refs(refs, entities, fuzzy_threshold=85.0)
    assert sorted(resolved) == ["abc123", "def456"]

def test_resolve_returns_unique_ids():
    """IDs duplicados no aparecen múltiples veces."""
    entities = {
        "abc123": {"label": "ORG", "text": "textFlow", "confidence": 0.9},
    }
    refs = ["textFlow", "TEXTFLOW", "textFlow."]
    resolved = resolve_entity_refs(refs, entities, fuzzy_threshold=85.0)
    assert resolved == ["abc123"]
```

**Step 3:** Crear el archivo de tests.

**Step 4:** Ejecutar (debe fallar hasta que se implemente A1):
```bash
pytest cmd/completion-worker/tests/test_entity_id_refs.py -v
```

---

### Task A3 — Integrar `resolve_entity_refs` en `finalize_job`

**Archivo:** `cmd/completion-worker/worker.py`

**Step 5:** Añadir import:
```python
from pkg.worker_common.entity_utils import resolve_entity_refs
```

**Step 6:** En `finalize_job`, tras enriquecer inferencias con embeddings (línea ~503-508), añadir:

```python
# Resuelve entity_refs -> entity_id_refs para cada inferencia
for chunk_id, chunk_data in chunks_with_inferences.items():
    for idx, inference in enumerate(chunk_data.get("inferences", [])):
        resolved_ids = resolve_entity_refs(
            inference.get("entity_refs", []),
            entities_dict,
            fuzzy_threshold=settings.fuzzy_match_threshold
        )
        if resolved_ids:
            inference["entity_id_refs"] = resolved_ids
        # else: no añadir la clave
```

**Step 7:** Verificar que `settings.fuzzy_match_threshold` exista (crearlo en `Settings` si no):
```python
fuzzy_match_threshold: float = 85.0
```

**Step 8:** Ejecutar tests del completion-worker:
```bash
pytest cmd/completion-worker/tests/ -v
```

---

### Task A4 — Actualizar modelo Go `InferenceItem`

**Archivo:** `internal/models/job.go:83-90`

**Step 9:** Añadir `EntityIDs` al struct:
```go
type InferenceItem struct {
    Text       string    `json:"text"`
    Confidence float32   `json:"confidence"`
    EntityRefs []string  `json:"entity_refs,omitempty"`
    EntityID   string    `json:"entity_id,omitempty"`       // deprecated, mantenido por compatibilidad
    EntityIDs  []string  `json:"entity_id_refs,omitempty"`  // NEW; nil si vacío (nunca []string{})
    Embedding  []float32 `json:"embedding,omitempty"`
}
```

**Step 10:** Verificar que todos los puntos donde se unmarshalla `InferenceItem` gestionen el nuevo campo (buscar con grep por `InferenceItem`).

**Step 11:** Ejecutar tests Go:
```bash
go test ./internal/models/...
```

---

### Task A5 — Añadir `schema_version` al results

**Archivos:** `cmd/completion-worker/worker.py` (zona `finalize_job`); `internal/models/job.go` (`JobResults` struct)

**Step 12:** En `finalize_job`, añadir `"schema_version": "1.1.0"` al dict `results`.

**Step 13:** En el modelo Go, en `JobResults` (`internal/models/job.go:97-108`), añadir:
```go
SchemaVersion string `json:"schema_version,omitempty"`
```

**Step 14:** Tests.

---

## Fase B: Nuevos endpoints en el orchestrator Go

### Task B1 — Crear `cmd/orchestrator/handlers/results.go`

**Archivo:** Crear `cmd/orchestrator/handlers/results.go`

Contiene todos los nuevos handlers.

**Optimización de pre-allocación** (aplicar en `toGraphView`):

```go
func toGraphView(results *models.JobResults) *GraphResponse {
    totalInferences := 0
    for _, chunk := range results.Chunks {
        totalInferences += len(chunk.Inferences)
    }

    // Capacidad aproximada con margen del 10% para evitar re-allocation
    capacityNodes := int(float64(1+len(results.Chunks)+len(results.Entities)+totalInferences) * 1.1)
    capacityEdges := int(float64(len(results.Chunks)+len(results.Entities)+totalInferences*2) * 1.1)

    nodes := make([]Node, 0, capacityNodes)
    edges := make([]Edge, 0, capacityEdges)

    // Document node
    docID := "doc_" + results.JobID
    nodes = append(nodes, Node{
        ID:    docID,
        Label: "Document",
        Props: map[string]any{"title": results.DocumentMetadata.Title},
    })

    // Chunks
    for _, chunk := range results.Chunks {
        nodes = append(nodes, Node{
            ID:    chunk.ChunkID,
            Label: "Chunk",
            Props: map[string]any{"index": chunk.Index, "text_length": len(chunk.Text)},
        })
        edges = append(edges, Edge{From: docID, To: chunk.ChunkID, Type: "HAS_CHUNK"})
    }

    // Entities
    for entityID, entity := range results.Entities {
        nodes = append(nodes, Node{
            ID:    entityID,
            Label: "Entity",
            Props: map[string]any{"label": entity.Label, "text": entity.Text, "confidence": entity.Confidence},
        })
        edges = append(edges, Edge{From: docID, To: entityID, Type: "HAS_ENTITY"})
    }

    // Inferences
    for _, chunk := range results.Chunks {
        for idx, inf := range chunk.Inferences {
            infID := "inf_" + chunk.ChunkID + "_" + string(rune('0'+idx))
            nodes = append(nodes, Node{
                ID:    infID,
                Label: "Inference",
                Props: map[string]any{"text": inf.Text, "confidence": inf.Confidence},
            })
            edges = append(edges, Edge{From: chunk.ChunkID, To: infID, Type: "HAS_INFERENCE"})
            // REFERS_TO edges por cada entity_id_refs (solo si existe y no vacío)
            for _, refID := range inf.EntityIDs {
                edges = append(edges, Edge{From: infID, To: refID, Type: "REFERS_TO"})
            }
        }
    }

    return &GraphResponse{
        SchemaVersion: results.SchemaVersion,
        JobID:         results.JobID,
        Nodes:         nodes,
        Edges:         edges,
    }
}
```

**Step 15:** Crear el archivo con la estructura de base.

**Step 16:** Implementar `loadJobResults(jobID string) *models.JobResults` que lee de `cfg.ResultsPath/{job_id}.json`.

---

### Task B2 — Implementar vectorsHandler, entitiesHandler, inferencesHandler

**Step 17:** Implementar en `cmd/orchestrator/handlers/results.go`:

**vectorsHandler:**
```go
func vectorsHandler(c *gin.Context) {
    // Supporta ?embeddings=false, ?fields=chunks,inferences, ?page=1&limit=100
}
```

**entitiesHandler:**
```go
func entitiesHandler(c *gin.Context) {
    // Devuelve lista plana de entities desde results.Entities
}
```

**inferencesHandler:**
```go
func inferencesHandler(c *gin.Context) {
    // Devuelve lista plana de inferences con entity_id_refs
}
```

---

### Task B3 — Query params en `/download`

**Archivo:** `cmd/orchestrator/main.go:1161-1317` (`downloadHandler`)

**Step 18:** Extraer helpers compartidos (filterFields, paginateChunks, paginateInferences) y aplicar `?embeddings=false` y `?fields=...`.

---

### Task B4 — Registrar routes en `setupRouter`

**Archivo:** `cmd/orchestrator/main.go:287-319`

**Step 19:**
```go
r.GET("/v1/documents/:id/graph", graphHandler)
r.GET("/v1/documents/:id/vectors", vectorsHandler)
r.GET("/v1/documents/:id/entities", entitiesHandler)
r.GET("/v1/documents/:id/inferences", inferencesHandler)
```

---

### Task B5 — Tests Go para los nuevos handlers

Crear `cmd/orchestrator/handlers/results_test.go` con tests para `toGraphView` y cada handler.

Test de capacidad de memoria:
```go
func TestToGraphViewPreallocatedCapacity(t *testing.T) {
    results := generateLargeMockResults() // 1000 chunks, 500 entidades, 5000 inferencias
    resp := toGraphView(results)

    // Verificar que len <= cap (slices pre-allocados)
    if len(resp.Nodes) > cap(resp.Nodes) {
        t.Errorf("Nodes len %d exceeds cap %d", len(resp.Nodes), cap(resp.Nodes))
    }
    if len(resp.Edges) > cap(resp.Edges) {
        t.Errorf("Edges len %d exceeds cap %d", len(resp.Edges), cap(resp.Edges))
    }

    // Verificar conteos esperados
    totalInferences := 0
    for _, chunk := range results.Chunks {
        totalInferences += len(chunk.Inferences)
    }
    expectedNodes := 1 + len(results.Chunks) + len(results.Entities) + totalInferences
    if len(resp.Nodes) != expectedNodes {
        t.Errorf("Expected %d nodes, got %d", expectedNodes, len(resp.Nodes))
    }
}
```

**Step 20:** Tests:
```bash
go test ./cmd/orchestrator/handlers/... -v
```

---

## Fase C: Webhook enriquecido

### Task C1 — Añadir `webhook_payload_mode` en Settings

**Archivo:** `cmd/completion-worker/worker.py:49-64` (`Settings`)

**Step 21:**
```python
webhook_payload_mode: str = "minimal"  # "minimal" | "summary"
```

---

### Task C2 — Refactorizar `send_webhook` para soportar summary con circuit breaker

**Archivo:** `cmd/completion-worker/worker.py:154-198`

```python
MAX_WEBHOOK_ITEMS = 500

def _should_use_summary_payload(results: dict) -> bool:
    entity_count = len(results.get("entities", {}))
    inference_count = sum(
        len(chunk.get("inferences", []))
        for chunk in results.get("chunks", [])
    )
    return (entity_count + inference_count) <= MAX_WEBHOOK_ITEMS

# En send_webhook:
if settings.webhook_payload_mode == "summary" and _should_use_summary_payload(results):
    payload = build_summary_payload(job_id, status, results)
else:
    if settings.webhook_payload_mode == "summary":
        entity_count = len(results.get("entities", {}))
        inference_count = sum(len(chunk.get("inferences", [])) for chunk in results.get("chunks", []))
        logger.warning(
            f"Webhook payload too large for job {job_id}: "
            f"{entity_count} entities + {inference_count} inferences exceeds {MAX_WEBHOOK_ITEMS}. "
            "Falling back to minimal mode."
        )
    payload = build_minimal_payload(job_id, status, results)
```

**Step 22:** Implementar la lógica de fallback.

---

### Task C3 — Exponer `WEBHOOK_PAYLOAD_MODE` en docker-compose

**Archivo:** `deploy/docker/docker-compose.yml` (sección completion-worker)

**Step 23:**
```yaml
- WEBHOOK_PAYLOAD_MODE=summary
```

---

### Task C4 — Tests webhook

Crear `cmd/completion-worker/tests/test_webhook.py` si no existe.

**Step 24:**
```bash
pytest cmd/completion-worker/tests/test_webhook.py -v
```

---

## Fase D: Eventos Redis enriquecidos

### Task D1 — Modificar `publish_job_completed`

**Archivo:** `pkg/events_python.py:53-59`

**Step 25:**
```python
def publish_job_completed(
    self,
    job_id: str,
    download_url: str = "",
    summary: Optional[Dict[str, int]] = None,
    metadata: Optional[Dict] = None
) -> None:
    metadata = metadata or {}
    metadata.update({
        "schema_version": "1.1.0",
        "download_url": download_url,
        "summary": summary or {},
    })
    self.publish_event(job_id, "job_completed", progress=100, status="completed", metadata=metadata)
```

---

### Task D2 — Actualizar llamadas en `finalize_job`

**Archivo:** `cmd/completion-worker/worker.py:547-549`

**Step 26:**
```python
self.event_bus.publish_job_completed(
    job_id,
    download_url=f"{settings.api_base_url}/v1/documents/{job_id}/download",
    summary={
        "chunks": len(chunks),
        "entities": len(entities_dict),
        "inferences": total_inferences,
    },
)
```

---

### Task D3 — Tests eventos

**Step 27:**
```bash
pytest pkg/tests/test_events_python.py -v
```

---

## Fase E: Documentación

### Task E1 — Actualizar Swagger/OpenAPI

**Archivo:** `cmd/orchestrator/docs/docs.go`

**Step 28:** Añadir documentación para los 4 nuevos endpoints y `schema_version`.

---

### Task E2 — Actualizar README

**Archivo:** `README.md`

**Step 29:** Añadir sección "Ingestion-ready output" con `/graph`, `/vectors`, webhooks y `entity_id_refs`.

---

## Fase F: Verificación end-to-end

### Task F1 — Build

```bash
make build
```

### Task F2 — Levantar stack

```bash
make docker-up
```

### Task F3 — E2E test

```bash
./bin/client -i README.md -o /tmp/v11_result.json -u http://localhost:9080 -f --timeout 5m
```

### Task F4 — Verificar nuevos endpoints

```bash
curl http://localhost:9080/v1/documents/{job_id}/graph | jq '.schema_version'
curl http://localhost:9080/v1/documents/{job_id}/vectors?embeddings=false | jq '.inferences[0].entity_id_refs'
curl http://localhost:9080/v1/documents/{job_id}/download?embeddings=false | jq '.schema_version'
```

### Task F5 — Verificar webhook

Configurar `WEBHOOK_PAYLOAD_MODE=summary`, enviar job con `webhook_url`, confirmar que el POST recibido incluye `results.entities` e `results.inferences`.

---

## Checklist de aceptación final

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

---

## Orden de ejecución recomendado

```
A1 → A2 → A3 → A4 → A5 → B1 → B2 → B3 → B4 → B5 → C1 → C2 → C3 → C4 → D1 → D2 → D3 → E1 → E2 → F1 → F2 → F3 → F4 → F5
```

**Nota:** Las fases A y B son independientes una vez creado `entity_utils.py`. Las fases C y D son paralelas entre sí pero dependen de la Fase A (completion-worker actualizado con entity_id_refs y summary counts).
