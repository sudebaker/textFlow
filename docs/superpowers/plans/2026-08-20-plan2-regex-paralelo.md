# Plan 2: Paralelizar regex + GLiNER — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ejecutar la extracción regex en un thread paralelo a GLiNER dentro de entities-worker usando `concurrent.futures.ThreadPoolExecutor`, preservando el degrade silencioso actual y sin crear cola nueva.

**Architecture:** Hoy el flujo de `process_message` (`cmd/entities-worker/entities_worker.py:249-392`) corre GLiNER (CPU/GPU-bound, :281-326) y DESPUÉS, en serie, llama al microservicio regex vía HTTP (`_extract_regex_entities`, :225-244 — I/O-bound, libera el GIL). Se extrae la orquestación a una función pura `extract_regex_parallel(text, regex_fn, gliner_fn)` que despacha regex con `ThreadPoolExecutor(max_workers=1)` y ejecuta `gliner_fn()` en el caller, mergeando resultados tras `future.result()`. `process_message` envuelve los loops GLiNER en el closure `gliner_extract()` y pasa el `:text` fetchado temprano. Se agregan 3 settings pydantic (`regex_service_url`, `regex_timeout`, `regex_enabled`) para reemplazar el `os.getenv` module-level y el timeout hardcodeado.

**Tech Stack:** Python 3.11, pydantic-settings, `concurrent.futures` (stdlib), pytest, make test-python.

## Global Constraints

- Air-gapped: no agregar dependencias nuevas. `concurrent.futures` y `ThreadPoolExecutor` son stdlib.
- Imports Python en 3 secciones: stdlib / third-party / local, orden alfabético (aplica `make format` = black + isort).
- Nombres: funciones snake_case, constantes UPPER_SNAKE, clases PascalCase.
- No se tocan colas RabbitMQ ni sus argumentos (`declareQueue` args quedan intactos).
- `sliding_window.merge_entities` NO se toca.
- El degrade silencioso actual (`_extract_regex_entities` retorna `[]` ante `requests.RequestException` o excepción genérica) se preserva; `extract_regex_parallel` además tolera que `regex_fn` lance.
- Suites válidas (fallos pre-existentes documentados, NO atribuibles a este plan): correr por-worker `pytest cmd/entities-worker/tests -v`. No correr `make test-python` global (falla por collection errors cross-worker + prometheus registry en `test_finalize_job.py`, pre-existentes en `8eade4c`).

---

## File Structure

- **Create:** `cmd/entities-worker/tests/test_regex_parallel.py` — unit tests de `extract_regex_parallel` (función pura, sin instanciar el worker) + test de integración de `process_message` con stubs vía `EntitiesWorker.__new__`.
- **Modify:** `cmd/entities-worker/entities_worker.py` — agregar `extract_regex_parallel`, settings de regex, y reestructurar `process_message` (fetch `:text` temprano, loops GLiNER en closure, join de futures).
- **Modify:** `cmd/entities-worker/app/config/settings.py` — agregar `regex_service_url`, `regex_timeout`, `regex_enabled`.
- **Modify:** `AGENTS.md` — documentar el patrón de threading regex.

---

## Task 1: Agregar settings de regex + función pura `extract_regex_parallel` + tests

**Files:**
- Modify: `cmd/entities-worker/app/config/settings.py`
- Modify: `cmd/entities-worker/entities_worker.py`
- Create: `cmd/entities-worker/tests/test_regex_parallel.py`

**Interfaces:**
- Consumes: `Settings` (pydantic BaseSettings) existente en `app/config/settings.py`; `_extract_regex_entities(text: str) -> list` existente en `entities_worker.py:225-244`.
- Produces:
  - `Settings.regex_service_url: str = "http://regex-entity-extractor:8081"`
  - `Settings.regex_timeout: int = 30`
  - `Settings.regex_enabled: bool = True`
  - `extract_regex_parallel(text: str, regex_fn: Optional[Callable[[str], list]], gliner_fn: Callable[[], list]) -> list` — despacha `regex_fn(text)` en thread; retorna `gliner_fn()` + resultados regex (o solo GLiNER si `text` vacío, `regex_fn is None` o `regex_fn` lanza). Firmas exactas de tests en Task 1.

- [ ] **Step 1: Escribir los tests que fallan — `cmd/entities-worker/tests/test_regex_parallel.py`**

```python
"""Unit tests for extract_regex_parallel and regex settings wiring."""

import json
import time
from unittest.mock import MagicMock

import entities_worker as ew


def test_merges_regex_and_gliner_results():
    gliner = lambda: [{"label": "PER", "text": "Juan"}]
    regex = lambda text: [{"label": "LOC", "text": "Madrid"}]

    result = ew.extract_regex_parallel("Hola", regex, gliner)

    assert len(result) == 2


def test_runs_concurrently():
    def gliner():
        time.sleep(0.2)
        return ["g"]

    def regex(text):
        time.sleep(0.2)
        return ["r"]

    start = time.time()
    result = ew.extract_regex_parallel("Hola", regex, gliner)
    elapsed = time.time() - start

    assert result == ["g", "r"]
    assert elapsed < 0.35  # paralelo (~0.2s), no serial (~0.4s)


def test_degrades_silently_when_regex_raises():
    def regex(text):
        raise RuntimeError("boom")

    gliner = lambda: ["g"]

    result = ew.extract_regex_parallel("Hola", regex, gliner)

    assert result == ["g"]


def test_skips_regex_when_no_text():
    gliner = lambda: ["g"]
    regex = MagicMock(side_effect=AssertionError("must not be called"))

    result = ew.extract_regex_parallel("", regex, gliner)

    assert result == ["g"]
    regex.assert_not_called()


def test_skips_regex_when_regex_fn_none():
    gliner = lambda: ["g"]

    result = ew.extract_regex_parallel("Hola", None, gliner)

    assert result == ["g"]
```

- [ ] **Step 2: Correr los tests para verificar que fallan**

Run: `pytest cmd/entities-worker/tests/test_regex_parallel.py -v`
Expected: FAIL — `AttributeError: module 'entities_worker' has no attribute 'extract_regex_parallel'`

- [ ] **Step 3: Implementar — agregar imports y la función pura en `entities_worker.py`**

En la sección stdlib de imports (después de `import json`, antes de `from typing import ...`):

```python
from concurrent.futures import ThreadPoolExecutor
```

Nota: el orden exacto lo ajusta `make format` (black + isort). Agregar `Optional` y `Callable` al import de `typing`:

```python
from typing import Any, Callable, Dict, List, Optional
```

Agregar la función pura a nivel de módulo (antes de `class EntitiesWorker`):

```python
def extract_regex_parallel(
    text: str,
    regex_fn: Optional[Callable[[str], list]],
    gliner_fn: Callable[[], list],
) -> list:
    """Run regex extraction in a background thread concurrent with gliner_fn.

    Returns gliner_fn() results merged with regex results. Degrades silently:
    if regex_fn is None, text is empty, or regex_fn raises, only gliner_fn()
    results are returned.

    Args:
        text: Full document text (regex input). Fetched before dispatch.
        regex_fn: Callable taking text and returning a list of entities.
        gliner_fn: Callable taking no args; runs in the caller thread (GLiNER).

    Returns:
        Merged entity list (gliner results first, then regex results).
    """
    regex_future = None
    executor = ThreadPoolExecutor(max_workers=1)
    if text and regex_fn is not None:
        regex_future = executor.submit(regex_fn, text)
    entities = gliner_fn()
    if regex_future is not None:
        try:
            entities.extend(regex_future.result())
        except Exception:
            pass
    executor.shutdown(wait=True)
    return entities
```

- [ ] **Step 4: Agregar los settings de regex en `cmd/entities-worker/app/config/settings.py`**

En `class Settings`, después del bloque "Deduplication Configuration":

```python
    # Regex Entity Extractor Configuration
    regex_service_url: str = Field(
        default="http://regex-entity-extractor:8081",
        description="URL of the regex-entity-extractor microservice",
    )

    regex_timeout: int = Field(
        default=30, description="HTTP timeout in seconds for regex extraction"
    )

    regex_enabled: bool = Field(
        default=True, description="Enable regex entity extraction"
    )
```

- [ ] **Step 5: Correr los tests para verificar que pasan**

Run: `pytest cmd/entities-worker/tests/test_regex_parallel.py -v`
Expected: PASS (5 passed)

- [ ] **Step 6: Commit**

```bash
git add cmd/entities-worker/tests/test_regex_parallel.py cmd/entities-worker/entities_worker.py cmd/entities-worker/app/config/settings.py
git commit -m "feat(entities): add extract_regex_parallel helper + regex settings"
```

---

## Task 2: Cablear `extract_regex_parallel` en `process_message` (TDD)

**Files:**
- Modify: `cmd/entities-worker/entities_worker.py` (`__init__` ~:70, `_extract_regex_entities` :225-244, `process_message` :249-334)
- Create: `cmd/entities-worker/tests/test_regex_parallel.py` (agregar tests de integración)

**Interfaces:**
- Consumes: `extract_regex_parallel`, `Settings.regex_*` (Task 1); `entity_id` (ya importado); `ENTITIES_QUEUE`/`INFERENCES_QUEUE` module-level.
- Produces: `process_message` con mismo contrato (retorna `{"status": "success", "job_id", "entities"}`); el fetch de `:text` ocurre temprano y el regex corre en paralelo a GLiNER; `:entities_raw`, `:steps`, `job_progress` y fan-out de inferences quedan sin cambios de formato.

- [ ] **Step 1: Escribir el test de integración que falla (agregar al final de `test_regex_parallel.py`)**

```python
def _build_worker():
    """Build an EntitiesWorker without BaseWorker.__init__ (avoids Prometheus
    registry collisions and real Redis/RabbitMQ connections)."""
    worker = ew.EntitiesWorker.__new__(ew.EntitiesWorker)
    worker.logger = MagicMock()
    worker.default_entities = ["PER", "ORG", "LOC"]
    worker.regex_enabled = True
    worker.regex_service_url = "http://regex-entity-extractor:8081"
    worker.regex_timeout = 30
    worker.model = MagicMock()
    worker.model.predict_entities.return_value = []
    worker.redis_client = MagicMock()
    worker.event_bus = MagicMock()
    worker.jobs_total = MagicMock()
    worker._publish_to_queue = MagicMock()
    return worker


def test_process_message_merges_regex_into_entities_raw():
    worker = _build_worker()
    worker.redis_client.get.side_effect = lambda key: (
        json.dumps("Juan trabaja en Madrid")
        if key.endswith(":text")
        else None
    )
    worker._extract_regex_entities = lambda text: [
        {"text": "Madrid", "label": "LOC", "confidence": 1.0, "start": 0, "end": 0, "chunk_id": "c1"}
    ]
    message = {
        "job_id": "job-1",
        "chunks": [
            {"chunk_id": "c1", "text": "Juan trabaja en Madrid", "start_offset": 0}
        ],
    }

    result = worker.process_message(message)

    assert result["status"] == "success"
    writes = {c[0][0]: c[0][1] for c in worker.redis_client.set.call_args_list}
    entities_raw = json.loads(writes["orchestrator:job:job-1:entities_raw"])
    assert any(e["label"] == "LOC" and e["text"] == "Madrid" for e in entities_raw)
    assert all("entity_id" in e for e in entities_raw)


def test_process_message_skips_regex_when_disabled():
    worker = _build_worker()
    worker.regex_enabled = False
    worker.redis_client.get.side_effect = lambda key: (
        json.dumps("Juan trabaja en Madrid") if key.endswith(":text") else None
    )
    worker._extract_regex_entities = lambda text: [
        {"text": "Madrid", "label": "LOC", "confidence": 1.0, "start": 0, "end": 0, "chunk_id": "c1"}
    ]
    message = {
        "job_id": "job-2",
        "chunks": [
            {"chunk_id": "c1", "text": "Juan trabaja en Madrid", "start_offset": 0}
        ],
    }

    worker.process_message(message)

    writes = {c[0][0]: c[0][1] for c in worker.redis_client.set.call_args_list}
    entities_raw = json.loads(writes["orchestrator:job:job-2:entities_raw"])
    assert entities_raw == []  # sin regex: solo GLiNER (mock devuelve [])
```

- [ ] **Step 2: Correr los tests para verificar que fallan**

Run: `pytest cmd/entities-worker/tests/test_regex_parallel.py -v`
Expected: FAIL — `AttributeError: 'EntitiesWorker' object has no attribute 'regex_service_url'` (porque `__init__` aún no lo setea; el test usa `__new__`).

- [ ] **Step 3: Cablear settings y reestructurar `process_message`**

En `EntitiesWorker.__init__` (después de `self.default_entities = ...` en ~:72):

```python
        self.regex_enabled = app_settings.regex_enabled
        self.regex_service_url = app_settings.regex_service_url
        self.regex_timeout = app_settings.regex_timeout
```

En `_extract_regex_entities` (:225-228), reemplazar la construcción del payload para usar los settings:

```python
    def _extract_regex_entities(self, text: str) -> list:
        try:
            payload = {"text": text}
            response = requests.post(
                f"{self.regex_service_url}/preprocess",
                json=payload,
                timeout=self.regex_timeout,
            )
            response.raise_for_status()
```

Reestructurar `process_message` — reemplazar el bloque regex serializado actual (:328-334):

```python
        try:
            text = self.redis_client.get(f"orchestrator:job:{job_id}:text")
        except Exception as e:
            self.logger.warning(f"Failed to read document text: {e}")
            text = None

        def gliner_extract() -> list:
            all_entities = []
            for chunk_id, chunk_text, chunk_offset in large_chunks:
                try:
                    def predict_with_thresholds(text, entity_types, threshold=0.1):
                        return self.model.predict_entities(text, entity_types, threshold=threshold)
                    entities_items = process_with_sliding_window(chunk_text, predict_with_thresholds, entity_types, threshold=0.1)
                    for e in entities_items:
                        label = e.get("label", "")
                        score = e.get("score", 0.0)
                        threshold_val = ENTITY_THRESHOLDS.get(label, 0.5)
                        if score >= threshold_val:
                            g_start, g_end = self.calculate_global_position(chunk_offset, e.get("start", 0), e.get("end", 0))
                            all_entities.append({"text": e.get("text", ""), "label": label, "confidence": float(score), "start": g_start, "end": g_end, "chunk_id": chunk_id})
                except Exception as e:
                    self.logger.warning(f"Error extracting entities from large chunk {chunk_id}: {e}")

            for batch_start in range(0, len(batch_chunks), GLINER_BATCH_SIZE):
                batch = batch_chunks[batch_start:batch_start + GLINER_BATCH_SIZE]
                texts = [c[1] for c in batch]
                try:
                    batch_predictions = self.model.predict_entities(texts, entity_types, threshold=0.1)
                    for (chunk_id, chunk_text, chunk_offset), entities_items in zip(batch, batch_predictions):
                        if entities_items and isinstance(entities_items[0], list):
                            entities_items = entities_items[0]
                        for e in entities_items:
                            label = e.get("label", "")
                            score = e.get("score", 0.0)
                            threshold_val = ENTITY_THRESHOLDS.get(label, 0.5)
                            if score >= threshold_val:
                                g_start, g_end = self.calculate_global_position(chunk_offset, e.get("start", 0), e.get("end", 0))
                                all_entities.append({"text": e.get("text", ""), "label": label, "confidence": float(score), "start": g_start, "end": g_end, "chunk_id": chunk_id})
                except Exception as e:
                    self.logger.warning(f"Batch prediction failed: {e}")
                    for chunk_id, chunk_text, chunk_offset in batch:
                        try:
                            entities = self.model.predict_entities(chunk_text, entity_types, threshold=0.1)
                            if entities and isinstance(entities[0], list):
                                entities = entities[0]
                            for e in entities:
                                label = e.get("label", "")
                                score = e.get("score", 0.0)
                                threshold_val = ENTITY_THRESHOLDS.get(label, 0.5)
                                if score >= threshold_val:
                                    g_start, g_end = self.calculate_global_position(chunk_offset, e.get("start", 0), e.get("end", 0))
                                    all_entities.append({"text": e.get("text", ""), "label": label, "confidence": float(score), "start": g_start, "end": g_end, "chunk_id": chunk_id})
                        except Exception as inner_e:
                            self.logger.warning(f"Error extracting entities from chunk {chunk_id}: {inner_e}")
            return all_entities

        regex_fn = self._extract_regex_entities if self.regex_enabled else None
        all_entities = extract_regex_parallel(text, regex_fn, gliner_extract)
```

Nota: el bloque `entities_key = f"orchestrator:job:{job_id}:entities_raw"` y todo lo posterior (:336-392) queda sin cambios — opera sobre `all_entities` ya mergeado.

- [ ] **Step 4: Correr los tests para verificar que pasan**

Run: `pytest cmd/entities-worker/tests/test_regex_parallel.py -v`
Expected: PASS (7 passed — 5 unit + 2 integración)

- [ ] **Step 5: Correr la suite completa del worker**

Run: `pytest cmd/entities-worker/tests -v`
Expected: PASS (todos los tests de entities-worker, incluyendo `test_api.py`, `test_entity_id.py`, `test_entity_types_parsing.py`)

- [ ] **Step 6: Formato e import check**

Run: `make format`
Expected: black + isort aplicados sin errores. Revisar con `git diff` que los imports quedaron en 3 secciones alfabéticas y que `entities_worker.py` solo cambió en las zonas previstas.

- [ ] **Step 7: Commit**

```bash
git add cmd/entities-worker/entities_worker.py cmd/entities-worker/tests/test_regex_parallel.py
git commit -m "perf(entities): run regex extraction in parallel thread with GLiNER"
```

---

## Task 3: Documentar el patrón en AGENTS.md

**Files:**
- Modify: `AGENTS.md`

**Interfaces:**
- Consumes: nada (documentación).

- [ ] **Step 1: Agregar nota en AGENTS.md (sección "Architecture" / DAG)**

Agregar bajo el bloque "### DAG del pipeline (IMPORTANTE)":

```markdown
### Entities-worker: regex en thread paralelo (D2)

`entities_worker.py:extract_regex_parallel()` ejecuta la extracción regex
(microservicio Go vía HTTP, I/O-bound) en un `ThreadPoolExecutor(max_workers=1)`
concurrente a GLiNER (CPU/GPU-bound). El `:text` se fetcha antes del dispatch.
Degrade silencioso: si el servicio regex falla, se retorna solo GLiNER. Control:
`REGEX_ENABLED` (default true), `REGEX_SERVICE_URL`, `REGEX_TIMEOUT` (vía
`app/config/settings.py`).
```

- [ ] **Step 2: Verificar que no hay cambios no deseados**

Run: `git diff --stat`
Expected: solo `AGENTS.md` modificado en esta tarea.

- [ ] **Step 3: Commit**

```bash
git add AGENTS.md
git commit -m "docs: document regex parallel threading in entities-worker"
```

---

## Self-Review (plan 2)

- **Spec coverage (D2, spec C:249 + Fase 2.3):** "Paralelizar regex + GLiNER con ThreadPoolExecutor dentro de entities-worker. NO cola" → Task 2 (thread paralelo, sin cola). "regex es I/O-bound y libera el GIL → paralelismo real" → `extract_regex_parallel` + test `test_runs_concurrently`. "Degrade silencioso ya existe" → preservado en Task 2 (regex_fn None/vacío/exception) y test `test_degrades_silently_when_regex_raises`. Sin cola nueva → no se toca `declareQueue`.
- **Placeholder scan:** sin TBD/TODO; cada paso tiene código concreto o comando exacto.
- **Type consistency:** `extract_regex_parallel(text, regex_fn, gliner_fn)` con `regex_fn: Optional[Callable[[str], list]]`; en Task 2 se pasa `self._extract_regex_entities if self.regex_enabled else None` (firma `(text: str) -> list`, coincide). `process_message` retorna el mismo dict de siempre.