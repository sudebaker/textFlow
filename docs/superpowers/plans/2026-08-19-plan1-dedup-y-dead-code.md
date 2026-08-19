# Plan 1: Unificar deduplicación de entidades + eliminar dead code — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unificar `deduplicate_entities` en `pkg/worker_common/entity_utils.py` con semántica de completion-worker, eliminar la versión duplicada de entities-worker, unificar `entity_id` con unidecode, y borrar el dead code `internal/pipeline/`.

**Architecture:** El paquete compartido `pkg/worker_common/entity_utils.py` ya contiene `normalize_entity_text`, `fuzzy_match_score` y `resolve_entity_refs`. Se agregan dos funciones puras: `deduplicate_entities(entities, threshold)` (dict por entity_id, semántica `start_offset`/`end_offset`) y `entity_id(label, text)` (sha256 de `label:unidecode(text).lower().strip()`, 12 hex). completion-worker importa y delega; entities-worker solo importa `entity_id` y elimina su lógica de dedup. `internal/pipeline/orchestrator.go` (0 callers) se elimina completo.

**Tech Stack:** Python 3.11 (rapidfuzz, unidecode, hashlib), Go 1.23, pytest, make test-python / go build.

## Global Constraints

- No internet en build ni runtime (air-gapped) — no agregar dependencias nuevas.
- Imports Python en 3 secciones: stdlib / third-party / local, orden alfabético.
- Nombres: funciones snake_case, constantes UPPER_SNAKE.
- No romper tests existentes: los 6 tests de `test_finalize_job.py` que cubren dedup deben seguir pasando (actualizados al import nuevo).
- RabbitMQ queue args: NO se tocan colas en este plan.
- `sliding_window.merge_entities` NO se toca (dedup posicional por overlap, concern distinto).
- Todos los binarios Go se buildean en `bin/`.

---

## File Structure

- **Modify:** `pkg/worker_common/entity_utils.py` — agregar `entity_id()` y `deduplicate_entities()` (función pura).
- **Modify:** `cmd/completion-worker/completion_worker.py` — importar ambas funciones, eliminar método `deduplicate_entities` y la inner `_normalize`/`_generate_id`.
- **Modify:** `cmd/entities-worker/entities_worker.py` — eliminar método `deduplicate_entities`, método `normalize_entity_text`, bloque `if DEDUPLICATION_ENABLED`, constantes muertas, imports de `unidecode`/`fuzz`; importar `entity_id` desde entity_utils.
- **Modify:** `cmd/completion-worker/tests/test_finalize_job.py` — importar `deduplicate_entities` desde entity_utils (los 6 tests dedup pasan a llamar la función pura).
- **Modify:** `cmd/entities-worker/tests/test_entity_id.py` — importar `entity_id` desde entity_utils.
- **Create:** `cmd/completion-worker/tests/test_entity_dedup.py` — tests de la función unificada. Se ubica en `cmd/*/tests` (no en `pkg/`) porque `make test-python` ejecuta solo `pytest cmd/*/tests -v`; el patrón del repo ya ubica tests de `entity_utils` en `cmd/completion-worker/tests/test_entity_id_refs.py`.
- **Delete:** `internal/pipeline/orchestrator.go` (y directorio `internal/pipeline/`).
- **Modify:** `README.md` — corregir regex-entity-extractor (es Go, no Python).
- **Modify:** `AGENTS.md` — documentar que el DAG real vive en Python y que `internal/pipeline` fue eliminado.

---

## Task 1: Agregar `entity_id()` y `deduplicate_entities()` a `pkg/worker_common/entity_utils.py`

**Files:**
- Modify: `pkg/worker_common/entity_utils.py`
- Create: `cmd/completion-worker/tests/test_entity_dedup.py`
- Modify: `cmd/completion-worker/tests/test_finalize_job.py` (tests dedup)

**Interfaces:**
- Consumes: `normalize_entity_text` y `fuzzy_match_score` existentes en `entity_utils.py`.
- Produces:
  - `entity_id(label: str, text: str) -> str` — sha256 hex 12 chars de `f"{label}:{normalize_entity_text(text)}"`. Usa `normalize_entity_text` (ya incluye unidecode + remove punct + lower + strip).
  - `deduplicate_entities(entities: list, threshold: float = 0.85) -> dict` — dict `{entity_id: {label, text, confidence, start_offset, end_offset, chunk_id}}`. Mergea cuando mismo label y `fuzzy_match_score/100 >= threshold`; el de mayor confidence gana offsets. Fallback de ID con `entity_id()` si falta `entity_id`.

- [ ] **Step 1: Escribir el test que falla — `cmd/completion-worker/tests/test_entity_dedup.py`**

Este archivo es nuevo; el conftest de completion-worker (`cmd/completion-worker/tests/conftest.py`) ya agrega PROJECT_ROOT al sys.path, así que `from pkg.worker_common.entity_utils import ...` resuelve correctamente.

```python
"""Unit tests for entity_utils.deduplicate_entities and entity_id."""

from pkg.worker_common.entity_utils import deduplicate_entities, entity_id


def test_entity_id_deterministic():
    a = entity_id("PER", "María García")
    b = entity_id("PER", "  María García  ")
    c = entity_id("PER", "maría garcía")
    assert a == b == c
    assert len(a) == 12


def test_entity_id_unidecode():
    # unidecode aplicado: "á" == "a"
    assert entity_id("PER", "María") == entity_id("PER", "Maria")


def test_entity_id_different_label():
    assert entity_id("PER", "centro") != entity_id("ORG", "centro")


def test_dedup_empty_input():
    assert deduplicate_entities([]) == {}


def test_dedup_fallback_without_entity_id():
    entities = [
        {"label": "ORG", "text": "ACME", "confidence": 0.8},
        {"label": "ORG", "text": "ACME", "confidence": 0.9},
    ]
    result = deduplicate_entities(entities)
    assert len(result) == 1
    eid = list(result.keys())[0]
    assert len(eid) == 12
    assert result[eid]["confidence"] == 0.9  # highest wins


def test_dedup_identical_text_same_label_merges():
    entities = [
        {"label": "PER", "text": "María García", "confidence": 0.9, "entity_id": "aaa000000001"},
        {"label": "PER", "text": "María García", "confidence": 0.7, "entity_id": "aaa000000002"},
    ]
    result = deduplicate_entities(entities)
    assert len(result) == 1
    eid = list(result.keys())[0]
    assert result[eid]["confidence"] == 0.9


def test_dedup_accent_only_difference_merges():
    entities = [
        {"label": "ORG", "text": "Departamento de Educacion", "confidence": 0.8, "entity_id": "bbb000000001"},
        {"label": "ORG", "text": "Departamento de Educación", "confidence": 0.9, "entity_id": "bbb000000002"},
    ]
    result = deduplicate_entities(entities)
    assert len(result) == 1
    eid = list(result.keys())[0]
    assert result[eid]["confidence"] == 0.9


def test_dedup_different_text_stays_separate():
    entities = [
        {"label": "PER", "text": "María García", "confidence": 0.9, "entity_id": "ccc000000001"},
        {"label": "PER", "text": "Juan López", "confidence": 0.8, "entity_id": "ccc000000002"},
    ]
    result = deduplicate_entities(entities)
    assert len(result) == 2


def test_dedup_different_label_no_merge():
    entities = [
        {"label": "PER", "text": "Aragón", "confidence": 0.9, "entity_id": "ddd000000001"},
        {"label": "LOC", "text": "Aragón", "confidence": 0.8, "entity_id": "ddd000000002"},
    ]
    result = deduplicate_entities(entities)
    assert len(result) == 2


def test_dedup_offsets_preserved_from_highest_confidence():
    entities = [
        {
            "label": "PER", "text": "María", "confidence": 0.7,
            "entity_id": "eee000000001", "start": 10, "end": 15,
            "chunk_id": "chunk_001",
        },
        {
            "label": "PER", "text": "María", "confidence": 0.9,
            "entity_id": "eee000000002", "start": 100, "end": 105,
            "chunk_id": "chunk_002",
        },
    ]
    result = deduplicate_entities(entities)
    assert len(result) == 1
    eid = list(result.keys())[0]
    assert result[eid]["start_offset"] == 100
    assert result[eid]["end_offset"] == 105
    assert result[eid]["chunk_id"] == "chunk_002"
```

- [ ] **Step 3: Correr el test para verificar que falla**

Run: `pytest cmd/completion-worker/tests/test_entity_dedup.py -v`
Expected: FAIL con `ImportError: cannot import name 'deduplicate_entities'` o `AttributeError`.

- [ ] **Step 4: Escribir la implementación — agregar funciones a `entity_utils.py`**

Agregar `import hashlib` a los imports estándar, y estas funciones tras `fuzzy_match_score`:

```python
def entity_id(label: str, text: str) -> str:
    """Return a stable 12-char hex ID for a (label, text) pair.

    Uses normalize_entity_text (unidecode + remove punct + lower + strip) so
    accented variants ("María" / "Maria") and case differ only in normalization.
    """
    key = f"{label}:{normalize_entity_text(text)}"
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def deduplicate_entities(entities: list, threshold: float = 0.85) -> dict:
    """Deduplicate entities using fuzzy text matching, keeping highest confidence.

    Two entities merge when they share the same label AND their normalized texts
    are similar enough (fuzzy_match_score / 100 >= threshold). Normalization uses
    normalize_entity_text (unidecode + remove punct + lower + strip) so accented
    variants ("Educación" / "Educacion") are treated as identical.

    Args:
        entities: List of entity dicts, each expected to have:
            - entity_id (optional): stable 12-char hex ID
            - label, text, confidence

    Returns:
        Dict keyed by entity_id → {label, text, confidence, start_offset,
        end_offset, chunk_id}. Per-chunk fields (chunk_id, start, end) are
        preserved as start_offset, end_offset, chunk_id in the merged entity.
        Falls back to entity_id(label, text) if the field is missing.
    """
    if not entities:
        return {}

    result: dict = {}
    norm_index: dict = {}

    for ent in entities:
        label = ent.get("label", "")
        text = ent.get("text", "")
        confidence = ent.get("confidence", 0.0)
        norm_text = normalize_entity_text(text)

        matched_id = None
        for existing_id, existing_norm in norm_index.items():
            if result[existing_id]["label"] != label:
                continue
            similarity = fuzzy_match_score(norm_text, existing_norm) / 100.0
            if similarity >= threshold:
                matched_id = existing_id
                break

        if matched_id:
            if confidence > result[matched_id].get("confidence", 0):
                result[matched_id] = {
                    "label": label,
                    "text": text,
                    "confidence": confidence,
                    "start_offset": ent.get("start", 0),
                    "end_offset": ent.get("end", 0),
                    "chunk_id": ent.get("chunk_id", ""),
                }
                norm_index[matched_id] = norm_text
        else:
            eid = ent.get("entity_id") or entity_id(label, text)
            result[eid] = {
                "label": label,
                "text": text,
                "confidence": confidence,
                "start_offset": ent.get("start", 0),
                "end_offset": ent.get("end", 0),
                "chunk_id": ent.get("chunk_id", ""),
            }
            norm_index[eid] = norm_text

    return result
```

- [ ] **Step 5: Correr el test para verificar que pasa**

Run: `pytest cmd/completion-worker/tests/test_entity_dedup.py -v`
Expected: 9 tests PASS.

- [ ] **Step 6: Actualizar los 6 tests de completion-worker para usar la función pura**

En `cmd/completion-worker/tests/test_finalize_job.py`, reemplazar la importación y las llamadas. Las 6 funciones (`test_deduplicate_entities_fallback_without_entity_id`, `test_deduplicate_entities_fuzzy_identical_text`, `test_deduplicate_entities_fuzzy_similar_text`, `test_deduplicate_entities_fuzzy_different_text`, `test_deduplicate_entities_fuzzy_different_label_no_merge`, `test_entity_offsets_preserved`) dejan de llamar `worker.deduplicate_entities(entities)` y pasan a llamar la función pura importada.

Reemplazar `worker = _make_worker()` y `worker.deduplicate_entities(entities)` por un import directo. Agregar al inicio del archivo:

```python
from pkg.worker_common.entity_utils import deduplicate_entities
```

Y en cada test dedup, reemplazar:

```python
    result = worker.deduplicate_entities(entities)
```

por:

```python
    result = deduplicate_entities(entities)
```

También eliminar la línea `worker = _make_worker()` dentro de los 6 tests dedup (ya no se usa el worker).

- [ ] **Step 7: Correr ambos suites de tests**

Run:
```bash
pytest cmd/completion-worker/tests -v
```
Expected: todos PASS (test_finalize_job mantiene sus tests no-dedup intactos, test_entity_dedup pasa).

- [ ] **Step 8: Commit**

```bash
git add pkg/worker_common/entity_utils.py cmd/completion-worker/tests/test_entity_dedup.py cmd/completion-worker/tests/test_finalize_job.py
git commit -m "feat(entity-utils): unify deduplicate_entities and entity_id in shared package"
```

---

## Task 2: Refactor completion-worker para delegar en entity_utils

**Files:**
- Modify: `cmd/completion-worker/completion_worker.py`

**Interfaces:**
- Consumes: `deduplicate_entities` y `entity_id` de `pkg.worker_common.entity_utils` (Task 1).
- Produces: `CompletionWorker.finalize_job` usa la función importada en `:711`; ya no existe método `deduplicate_entities`.

- [ ] **Step 1: Agregar imports desde entity_utils**

En `cmd/completion-worker/completion_worker.py`, sección local (tras `from pkg.worker_common.pubsub_base import BasePubSubWorker`):

```python
from pkg.worker_common.entity_utils import deduplicate_entities, entity_id
```

- [ ] **Step 2: Eliminar el método `deduplicate_entities`**

Eliminar el método completo `deduplicate_entities` de `completion_worker.py:384-463` (incluido su docstring). El caller en `:711` cambia de `self.deduplicate_entities(entities_raw)` a `deduplicate_entities(entities_raw)`.

- [ ] **Step 3: Limpiar imports muertos**

`fuzz` y `unidecode` ya no se usan en completion-worker (verificar con grep antes de eliminar). Si el grep confirma 0 usos fuera de la sección eliminada, eliminar:

```python
from rapidfuzz import fuzz
from unidecode import unidecode
```

Y `import hashlib` queda solo si se usa en otro lado (verificar).

Run:
```bash
rg -n "fuzz|unidecode|hashlib" cmd/completion-worker/completion_worker.py
```

- [ ] **Step 4: Correr tests de completion-worker**

Run: `pytest cmd/completion-worker/tests -v`
Expected: todos PASS.

- [ ] **Step 5: Commit**

```bash
git add cmd/completion-worker/completion_worker.py
git commit -m "refactor(completion-worker): delegate entity dedup to entity_utils"
```

---

## Task 3: Eliminar dedup duplicado y usar `entity_id` compartido en entities-worker

**Files:**
- Modify: `cmd/entities-worker/entities_worker.py`
- Modify: `cmd/entities-worker/tests/test_entity_id.py`

**Interfaces:**
- Consumes: `entity_id` de `pkg.worker_common.entity_utils` (Task 1).
- Produces: `process_message` escribe `entities_raw` en Redis SIN dedup previo; el dedup final queda exclusivamente en completion-worker. `sliding_window.merge_entities` intacta.

- [ ] **Step 1: Escribir test que falla — actualizar `test_entity_id.py`**

`cmd/entities-worker/tests/test_entity_id.py` importa `from entities_worker import entity_id`. Cambiar a:

```python
from pkg.worker_common.entity_utils import entity_id
```

Ejecutar para verificar que pasa:
Run: `pytest cmd/entities-worker/tests/test_entity_id.py -v`
Expected: PASS (entity_id ahora aplica unidecode; los asserts existentes siguen válidos).

- [ ] **Step 2: Agregar import de `entity_id` en entities_worker**

En `cmd/entities-worker/entities_worker.py`, agregar en la sección local:

```python
from pkg.worker_common.entity_utils import entity_id
```

- [ ] **Step 3: Eliminar la función module-level `entity_id`**

Eliminar `entity_id()` en `entities_worker.py:66-69` (ya importada de entity_utils).

- [ ] **Step 4: Eliminar método `normalize_entity_text`**

Eliminar `normalize_entity_text` (método de clase) en `entities_worker.py:235-236`.

- [ ] **Step 5: Eliminar método `deduplicate_entities`**

Eliminar el método completo `deduplicate_entities` en `entities_worker.py:238-263`.

- [ ] **Step 6: Eliminar el bloque `if DEDUPLICATION_ENABLED`**

En `entities_worker.py:376-377`, eliminar:

```python
        if DEDUPLICATION_ENABLED:
            all_entities = self.deduplicate_entities(all_entities)
```

- [ ] **Step 7: Limpiar constantes muertas**

Eliminar en `entities_worker.py:47-48`:

```python
DEDUPLICATION_ENABLED = app_settings.deduplication_enabled
FUZZY_MATCH_THRESHOLD = app_settings.fuzzy_match_threshold
```

Si `app_settings` queda sin otros usos, verificar antes de tocar. El bloque `from app.config.settings import Settings as AppSettings` y `app_settings = AppSettings()` se conservan (GLINER_MODEL_PATH y ENTITY_THRESHOLDS dependen de app_settings).

- [ ] **Step 8: Limpiar imports muertos**

Eliminar de los imports de entities_worker (verificar con grep antes):
```python
from unidecode import unidecode
from rapidfuzz import fuzz
```

Run: `rg -n "unidecode|fuzz\." cmd/entities-worker/entities_worker.py`
Expected: solo restan usos en `sliding_window.py` (intacto). Si `unidecode`/`fuzz` no se usan más en entities_worker.py, eliminar ambos imports.

- [ ] **Step 9: Verificar que `entity_id` se usa en `process_message`**

El bloque `:380-381` ya llama `entity_id(ent.get("label",""), ent.get("text",""))` — ahora resuelve al import de entity_utils. No requiere cambios.

- [ ] **Step 10: Correr tests de entities-worker**

Run: `pytest cmd/entities-worker/tests -v`
Expected: todos PASS (test_sliding_window y test_api no dependen de la dedup eliminada).

- [ ] **Step 11: Correr todos los tests de Python del repo**

Run: `make test-python`
Expected: todos PASS.

- [ ] **Step 12: Commit**

```bash
git add cmd/entities-worker/entities_worker.py cmd/entities-worker/tests/test_entity_id.py
git commit -m "refactor(entities-worker): remove duplicate dedup, use shared entity_id"
```

---

## Task 4: Eliminar dead code `internal/pipeline/`

**Files:**
- Delete: `internal/pipeline/orchestrator.go`
- Delete: `internal/pipeline/` (directorio)

**Interfaces:**
- Consumes: nada (verificación previa: 0 importers).
- Produces: el paquete `internal/pipeline` deja de existir.

- [ ] **Step 1: Verificar que no hay callers**

Run:
```bash
rg -n "internal/pipeline|textflow/internal/pipeline" --glob "*.go" cmd internal
```
Expected: solo `internal/pipeline/orchestrator.go` (la propia definición). Si aparece algún caller, parar y evaluar.

- [ ] **Step 2: Verificar que no hay tests ni referencias en Makefile/CI**

Run:
```bash
rg -n "internal/pipeline" Makefile .github/ deploy/ docker-compose.yml 2>/dev/null
```
Expected: sin matches.

- [ ] **Step 3: Eliminar el directorio**

Run:
```bash
rm -rf internal/pipeline
```

- [ ] **Step 4: Verificar compilación Go**

Run:
```bash
make build-orchestrator
go build ./...
```
Expected: compila sin errores (0 referencias al paquete eliminado).

- [ ] **Step 5: Correr tests Go**

Run: `make test`
Expected: todos PASS.

- [ ] **Step 6: Commit**

```bash
git add -A internal/pipeline
git commit -m "chore: remove dead code internal/pipeline (0 callers)"
```

---

## Task 5: Corregir documentación (README regex + AGENTS.md DAG real)

**Files:**
- Modify: `README.md`
- Modify: `AGENTS.md`

**Interfaces:**
- Consumes: nada.
- Produces: documentación correcta sobre regex (Go) y ubicación del DAG real (Python).

- [ ] **Step 1: Verificar las menciones incorrectas en README**

Run:
```bash
rg -n "regex-entity-extractor" README.md
```
Buscar las líneas que describen el servicio como Python (README.md:80,122 según auditoría).

- [ ] **Step 2: Corregir README**

En las líneas encontradas, cambiar la descripción de Python a Go (ej: "regex entity extraction service (Go)").

- [ ] **Step 3: Documentar en AGENTS.md**

Agregar en la sección Architecture de AGENTS.md:

```markdown
### DAG del pipeline (IMPORTANTE)

El DAG **no** vive en el orchestrator Go. Vive en Python:
- `cmd/extraction-worker/worker.py` — routing fan-out (embeddings/entities/metadata)
- `cmd/completion-worker/completion_worker.py` — `required_steps`

`internal/pipeline/` fue eliminado (dead code, 0 callers).
```

- [ ] **Step 4: Commit**

```bash
git add README.md AGENTS.md
git commit -m "docs: correct regex service language, document real pipeline DAG"
```

---

## Self-Review

**1. Spec coverage:**
- Fase 0.1 (D1): Tasks 1-3 cubren mover dedup a entity_utils, eliminar la de entities-worker, unificar entity_id con unidecode. ✅
- Fase 0.2: Task 5 (AGENTS.md documenta el DAG real). ✅
- Fase 1.5 (dead code): Task 4 elimina `ProcessInParallel`/`WaitForCompletion`/`internal/pipeline`. ✅
- Corregir README regex (Go): Task 5. ✅
- `sliding_window.merge_entities` intacta: no se toca en ninguna tarea. ✅
- Fase 0.3 (reescribir doc de mejoras) y 0.2 (mencionar EventBus/SSE/batch/cache existentes en AGENTS.md): parcial — Task 5 documenta el DAG pero no el resto de premisas ya-existen; queda como pendiente fuera de alcance (se hará en un commit de docs posterior o al ejecutar Fase 2).

**2. Placeholder scan:** Sin TBD/TODO. Todos los pasos tienen contenido concreto.

**3. Type consistency:**
- `deduplicate_entities(entities, threshold=0.85) -> dict`: definida en Task 1, usada en Task 2 (`:711`). ✅
- `entity_id(label, text) -> str`: definida en Task 1, usada en Task 2 y Task 3 (`:381`). ✅
- `normalize_entity_text` y `fuzzy_match_score` preexistentes se reusan, sin renombrar. ✅
- Tests importan `deduplicate_entities`/`entity_id` desde `pkg.worker_common.entity_utils` consistentemente. ✅

**Pendiente (fuera de este plan):** Fase 0.3 (reescribir `textFlow_mejoras_arquitectura_y_rendimiento.md` marcando los 9 ítems como ya-existe), que requiere consenso sobre el doc original.
