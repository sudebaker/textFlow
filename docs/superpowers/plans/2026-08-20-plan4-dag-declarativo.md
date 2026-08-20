# Plan 4: DAG declarativo (PipelineDefinition JSON) + pipeline_version — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrar el DAG hardcoded de los 2 espejos Python (routing en `extraction-worker/worker.py:1019-1037` + `required_steps` en `completion_worker.py:464-492`) a una `PipelineDefinition` declarativa en **JSON** (decisión del usuario: sin YAML ni dependencias nuevas, air-gapped), y añadir `pipeline_version` a `JobMessage` como escape hatch para dual-run futuro. Migración big-bang con drain.

**Architecture:** Se crea `configs/pipeline.json` (config default: pipeline `full` y `spreadsheet`, feature extra `inferences`, regla `audio_replaces_extraction`) y `pkg/worker_common/pipeline_config.py` (`PipelineDefinition.load()` + `queues_for()` + `steps_for()`). `extraction-worker` reemplaza su `if/else` de routing por `queues_for(is_spreadsheet, features)`; `completion-worker` reemplaza el bloque de `required_steps` por `steps_for(...)` y elimina `self.default_required_steps`/`self.spreadsheet_required_steps` (el espejo). En Go, `JobMessage` gana `PipelineVersion` (`json:"pipeline_version,omitempty"`) seteado a `"v1"` en los 3 constructores; `extraction-worker` lo reenvía en su `job_message`. Los workers lo leen pero lo ignoran si vale `"v1"` (escape hatch). El orchestrator Go NO necesita cargar la config (solo publica a `extract_text`) — añadirle un loader sería dead code (regla del repo).

**Tech Stack:** Go 1.23 (stdlib `encoding/json`), Python 3.11 (stdlib `json`), pytest, docker-compose.

## Global Constraints

- Air-gapped: no agregar dependencias nuevas. JSON usa `encoding/json` (Go) y `json` (Python) stdlib.
- Imports Python en 3 secciones: stdlib / third-party / local, orden alfabético.
- Nombres: funciones snake_case (Py), PascalCase (Go exportado), constantes UPPER_SNAKE.
- No se tocan colas RabbitMQ ni sus argumentos (`declareQueue` intacto).
- El DAG NO es `ProcessInParallel` (eliminado en Plan 1). No reimplementar nada en Go.
- Compat de comportamiento: los steps/queues resultantes deben ser idénticos a los actuales (spreadsheet→entities; default→embeddings+entities+metadata; +inferences si feature).
- Suites válidas: `make test` (Go) + por-worker pytest. NO `make test-python` global (fallos pre-existentes).
- Drain (D4): stop admissions → esperar `ZCard active_jobs == 0` (o `JobTimeout=60m`) → deploy → resume. Caveat documentado: `ExpireStuckJobs` solo expira job-level `pending`/`processing`/`extracting`.

---

## File Structure

- **Create:** `configs/pipeline.json` — config declarativa del DAG.
- **Create:** `pkg/worker_common/pipeline_config.py` — `PipelineDefinition` (loader + `queues_for` + `steps_for`).
- **Create:** `cmd/completion-worker/tests/test_pipeline_config.py` — unit tests del loader.
- **Modify:** `internal/models/job.go:156-166` — agregar `PipelineVersion`.
- **Modify:** `cmd/orchestrator/handlers/batch.go:172` y `cmd/orchestrator/main.go:535,1149` — setear `PipelineVersion: "v1"`.
- **Create/Modify:** `internal/models/job_test.go` (o existente) — test de round-trip JSON con `pipeline_version`.
- **Modify:** `cmd/extraction-worker/worker.py:1008-1049` — reenviar `pipeline_version` + routing vía `queues_for`.
- **Modify:** `cmd/completion-worker/completion_worker.py:90-102,464-492` — `steps_for`; eliminar el espejo.
- **Modify:** `deploy/docker/docker-compose.yml` + Dockerfiles de extraction/completion — exponer `configs/pipeline.json` (COPY + `PIPELINE_CONFIG_PATH`).
- **Modify:** `AGENTS.md` — runbook de drain + DAG declarativo.

---

## Task 1: `pipeline_version` en `JobMessage` (Go) + reenvío en extraction-worker

**Files:**
- Modify: `internal/models/job.go`
- Modify: `cmd/orchestrator/handlers/batch.go`
- Modify: `cmd/orchestrator/main.go`
- Create: `internal/models/job_test.go`
- Modify: `cmd/extraction-worker/worker.py`

**Interfaces:**
- Consumes: `models.JobMessage` (`internal/models/job.go:156`).
- Produces: `JobMessage.PipelineVersion string \`json:"pipeline_version,omitempty"\``; los 3 constructores setean `"v1"`; `extraction-worker` reenvía `pipeline_version` en su `job_message`.

- [ ] **Step 1: Escribir el test Go que falla — `internal/models/job_test.go`**

```go
package models

import (
	"encoding/json"
	"testing"
)

func TestJobMessagePipelineVersionRoundTrip(t *testing.T) {
	in := JobMessage{
		JobID:           "job-1",
		PipelineVersion: "v1",
	}
	data, err := json.Marshal(in)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var out JobMessage
	if err := json.Unmarshal(data, &out); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if out.PipelineVersion != "v1" {
		t.Fatalf("expected pipeline_version=v1, got %q", out.PipelineVersion)
	}
}

func TestJobMessagePipelineVersionOmittedWhenEmpty(t *testing.T) {
	in := JobMessage{JobID: "job-2"}
	data, err := json.Marshal(in)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	if string(data) != `{"job_id":"job-2"}` {
		t.Fatalf("expected omitempty job_id only, got %s", data)
	}
}
```

- [ ] **Step 2: Correr el test para verificar que falla**

Run: `go test ./internal/models/ -run TestJobMessagePipelineVersion -v`
Expected: FAIL — `unknown field "pipeline_version"` en unmarshal.

- [ ] **Step 3: Implementar — agregar el campo en `internal/models/job.go`**

En `type JobMessage struct` (:156-166), después de `Features`:

```go
	PipelineVersion string      `json:"pipeline_version,omitempty"`
```

- [ ] **Step 4: Setear `"v1"` en los 3 constructores**

- `cmd/orchestrator/handlers/batch.go:172` — agregar `PipelineVersion: "v1",` al literal.
- `cmd/orchestrator/main.go:535` — agregar `PipelineVersion: "v1",` al literal.
- `cmd/orchestrator/main.go:1149` — agregar `PipelineVersion: "v1",` al literal.

- [ ] **Step 5: Correr el test para verificar que pasa + build**

Run:
```bash
go test ./internal/models/ -run TestJobMessagePipelineVersion -v
make build-orchestrator
```
Expected: PASS + binario compilado en `bin/orchestrator`.

- [ ] **Step 6: Reenviar `pipeline_version` en extraction-worker**

En `cmd/extraction-worker/worker.py:1008-1012`:

```python
                job_message = {
                    "job_id": job_id,
                    "chunks": chunks,
                    "document_metadata": document_metadata,
                }
```

reemplazar por:

```python
                job_message = {
                    "job_id": job_id,
                    "chunks": chunks,
                    "document_metadata": document_metadata,
                    "pipeline_version": body.get("pipeline_version", "v1"),
                }
```

- [ ] **Step 7: Correr suite del worker + commit**

Run: `pytest cmd/extraction-worker/tests -v 2>&1 | tail -20` (si la suite existe)
Expected: PASS o suite inexistente (anotar).

```bash
git add internal/models/job.go internal/models/job_test.go cmd/orchestrator/handlers/batch.go cmd/orchestrator/main.go cmd/extraction-worker/worker.py
git commit -m "feat(models): add pipeline_version to JobMessage as escape hatch"
```

---

## Task 2: `configs/pipeline.json` + `PipelineDefinition` loader + tests

**Files:**
- Create: `configs/pipeline.json`
- Create: `pkg/worker_common/pipeline_config.py`
- Create: `cmd/completion-worker/tests/test_pipeline_config.py`

**Interfaces:**
- Consumes: nada (archivo JSON nuevo).
- Produces:
  - `configs/pipeline.json` — schema: `version`, `default_pipeline{name,steps,publish_queues}`, `pipelines{spreadsheet{...}}`, `feature_extras{inferences{step,queue}}`, `rules{audio_replaces_extraction}`.
  - `class PipelineDefinition`:
    - `load(path: Optional[str] = None) -> PipelineDefinition` — path por defecto env `PIPELINE_CONFIG_PATH` o `/app/configs/pipeline.json`.
    - `queues_for(*, is_spreadsheet: bool, features: List[str]) -> List[str]`
    - `steps_for(*, is_spreadsheet: bool, is_audio: bool, features: List[str]) -> Set[str]`

- [ ] **Step 1: Crear `configs/pipeline.json`**

```json
{
  "version": "v1",
  "default_pipeline": {
    "name": "full",
    "steps": ["extraction", "embeddings", "entities", "metadata"],
    "publish_queues": ["embeddings", "entities", "metadata"]
  },
  "pipelines": {
    "spreadsheet": {
      "name": "spreadsheet",
      "steps": ["extraction", "entities"],
      "publish_queues": ["entities"]
    }
  },
  "feature_extras": {
    "inferences": {
      "step": "inferences",
      "queue": "inferences"
    }
  },
  "rules": {
    "audio_replaces_extraction": true
  }
}
```

- [ ] **Step 2: Escribir los tests que fallan — `cmd/completion-worker/tests/test_pipeline_config.py`**

```python
"""Unit tests for PipelineDefinition (declarative DAG config)."""

import json

from pkg.worker_common.pipeline_config import PipelineDefinition


CONFIG = {
    "version": "v1",
    "default_pipeline": {
        "name": "full",
        "steps": ["extraction", "embeddings", "entities", "metadata"],
        "publish_queues": ["embeddings", "entities", "metadata"],
    },
    "pipelines": {
        "spreadsheet": {
            "name": "spreadsheet",
            "steps": ["extraction", "entities"],
            "publish_queues": ["entities"],
        }
    },
    "feature_extras": {"inferences": {"step": "inferences", "queue": "inferences"}},
    "rules": {"audio_replaces_extraction": True},
}


def test_queues_for_default():
    pd = PipelineDefinition(CONFIG)
    assert pd.queues_for(is_spreadsheet=False, features=[]) == [
        "embeddings", "entities", "metadata",
    ]


def test_queues_for_spreadsheet():
    pd = PipelineDefinition(CONFIG)
    assert pd.queues_for(is_spreadsheet=True, features=[]) == ["entities"]


def test_queues_for_inferences_feature():
    pd = PipelineDefinition(CONFIG)
    assert pd.queues_for(is_spreadsheet=False, features=["inferences"]) == [
        "embeddings", "entities", "metadata", "inferences",
    ]


def test_steps_for_default():
    pd = PipelineDefinition(CONFIG)
    assert pd.steps_for(is_spreadsheet=False, is_audio=False, features=[]) == {
        "extraction", "embeddings", "entities", "metadata",
    }


def test_steps_for_spreadsheet():
    pd = PipelineDefinition(CONFIG)
    assert pd.steps_for(is_spreadsheet=True, is_audio=False, features=[]) == {
        "extraction", "entities",
    }


def test_steps_for_audio_replaces_extraction():
    pd = PipelineDefinition(CONFIG)
    assert pd.steps_for(is_spreadsheet=False, is_audio=True, features=[]) == {
        "audio", "embeddings", "entities", "metadata",
    }


def test_steps_for_inferences_feature():
    pd = PipelineDefinition(CONFIG)
    assert pd.steps_for(is_spreadsheet=False, is_audio=False, features=["inferences"]) == {
        "extraction", "embeddings", "entities", "metadata", "inferences",
    }


def test_load_from_file(tmp_path):
    p = tmp_path / "pipeline.json"
    p.write_text(json.dumps(CONFIG), encoding="utf-8")
    pd = PipelineDefinition.load(str(p))
    assert pd.version == "v1"
    assert pd.steps_for(is_spreadsheet=False, is_audio=False, features=[]) == {
        "extraction", "embeddings", "entities", "metadata",
    }
```

- [ ] **Step 3: Correr los tests para verificar que fallan**

Run: `pytest cmd/completion-worker/tests/test_pipeline_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pkg.worker_common.pipeline_config'`

- [ ] **Step 4: Implementar — `pkg/worker_common/pipeline_config.py`**

```python
"""PipelineDefinition loader for textFlow Python workers.

Reads configs/pipeline.json (JSON, no new dependencies) and exposes helpers
to derive routing queues (extraction-worker) and required steps
(completion-worker) for a job.
"""

import json
import os
from typing import Dict, List, Optional, Set

DEFAULT_CONFIG_PATH = "/app/configs/pipeline.json"


class PipelineDefinition:
    """Declarative DAG definition: pipelines, feature extras and rules."""

    def __init__(self, data: Dict):
        self.data = data
        self.version = data.get("version", "v1")
        self.default_pipeline = data["default_pipeline"]
        self.pipelines = data.get("pipelines", {})
        self.feature_extras = data.get("feature_extras", {})
        self.rules = data.get("rules", {})

    @classmethod
    def load(cls, path: Optional[str] = None) -> "PipelineDefinition":
        """Load a PipelineDefinition from a JSON file.

        Args:
            path: JSON file path. Defaults to PIPELINE_CONFIG_PATH env or
                /app/configs/pipeline.json.

        Returns:
            Loaded PipelineDefinition.

        Raises:
            FileNotFoundError: if the config file does not exist.
        """
        config_path = path or os.getenv("PIPELINE_CONFIG_PATH", DEFAULT_CONFIG_PATH)
        with open(config_path, "r", encoding="utf-8") as f:
            return cls(json.load(f))

    def queues_for(self, *, is_spreadsheet: bool, features: List[str]) -> List[str]:
        """Routing queues for extraction-worker.

        Args:
            is_spreadsheet: whether the document is a spreadsheet (entities-only).
            features: requested features (e.g. ["inferences"]).

        Returns:
            Ordered list of target queues to publish the job to.
        """
        base = (
            self.pipelines["spreadsheet"]["publish_queues"]
            if is_spreadsheet
            else self.default_pipeline["publish_queues"]
        )
        queues = list(base)
        for feature in features:
            extra = self.feature_extras.get(feature)
            if extra and extra.get("queue") and extra["queue"] not in queues:
                queues.append(extra["queue"])
        return queues

    def steps_for(
        self, *, is_spreadsheet: bool, is_audio: bool, features: List[str]
    ) -> Set[str]:
        """Required completion steps for completion-worker.

        Applies the audio_replaces_extraction rule and feature extra steps.

        Args:
            is_spreadsheet: whether the document is a spreadsheet.
            is_audio: whether the job produced an 'audio' step.
            features: requested features (e.g. ["inferences"]).

        Returns:
            Set of step names that must be completed before finalization.
        """
        base = list(self.default_pipeline["steps"])
        if is_spreadsheet:
            base = list(self.pipelines["spreadsheet"]["steps"])
        steps = set(base)
        if is_audio and self.rules.get("audio_replaces_extraction", True):
            steps.discard("extraction")
            steps.add("audio")
        for feature in features:
            extra = self.feature_extras.get(feature)
            if extra and extra.get("step"):
                steps.add(extra["step"])
        return steps
```

- [ ] **Step 5: Correr los tests para verificar que pasan**

Run: `pytest cmd/completion-worker/tests/test_pipeline_config.py -v`
Expected: PASS (9 passed)

- [ ] **Step 6: Commit**

```bash
git add configs/pipeline.json pkg/worker_common/pipeline_config.py cmd/completion-worker/tests/test_pipeline_config.py
git commit -m "feat(pkg): add declarative PipelineDefinition JSON config + loader"
```

---

## Task 3: Migrar routing de extraction-worker a `queues_for`

**Files:**
- Modify: `cmd/extraction-worker/worker.py:1019-1037`

**Interfaces:**
- Consumes: `PipelineDefinition.load` (Task 2).
- Produces: routing idéntico al actual (spreadsheet→`entities`; default→`embeddings,entities,metadata`; +`inferences` si feature).

- [ ] **Step 1: Escribir test de regresión (o confirmar cobertura existente)**

Verificar si `cmd/extraction-worker/tests/` cubre el routing. Si no hay suite de extraction-worker, añadir `cmd/extraction-worker/tests/test_routing.py` con la lógica pura de detección de spreadsheet + la lista esperada:

```python
"""Regression tests for extraction routing vs PipelineDefinition."""

from pkg.worker_common.pipeline_config import PipelineDefinition


def test_spreadsheet_routes_to_entities_only():
    pd = PipelineDefinition.load("configs/pipeline.json")
    assert pd.queues_for(is_spreadsheet=True, features=[]) == ["entities"]


def test_default_routes_to_three_stages():
    pd = PipelineDefinition.load("configs/pipeline.json")
    assert pd.queues_for(is_spreadsheet=False, features=[]) == [
        "embeddings", "entities", "metadata",
    ]


def test_inferences_feature_appends_queue():
    pd = PipelineDefinition.load("configs/pipeline.json")
    assert pd.queues_for(is_spreadsheet=False, features=["inferences"]) == [
        "embeddings", "entities", "metadata", "inferences",
    ]
```

(Nota: `PipelineDefinition.load("configs/pipeline.json")` usa ruta relativa al CWD del test; si pytest corre desde la raíz del repo, resuelve. Ajustar a `Path(__file__).parents[3] / "configs/pipeline.json"` si no.)

- [ ] **Step 2: Correr el test para verificar que pasa la config**

Run: `pytest cmd/extraction-worker/tests/test_routing.py -v` (crear `tests/conftest.py` si no existe, siguiendo el patrón de entities-worker)
Expected: PASS (la config ya produce los queues correctos).

- [ ] **Step 3: Implementar — reemplazar el routing en `worker.py`**

En `cmd/extraction-worker/worker.py:1019-1037`, reemplazar:

```python
                # Determine if this is a spreadsheet (reduce pipeline: entities only)
                is_spreadsheet = False
                if body.get("mime_type") == "application/spreadsheet":
                    is_spreadsheet = True
                elif body.get("document_path"):
                    path_lower = body["document_path"].lower()
                    if path_lower.endswith((".csv", ".xls", ".xlsx")):
                        is_spreadsheet = True

                # Route to appropriate queues
                features = body.get("features") or []
                if is_spreadsheet:
                    target_queues = ["entities"]
                    logger.info(f"Detected spreadsheet, routing to entities-only pipeline")
                else:
                    target_queues = ["embeddings", "entities", "metadata"]

                if "inferences" in features:
                    target_queues.append("inferences")
```

por:

```python
                # Determine if this is a spreadsheet (reduce pipeline: entities only)
                is_spreadsheet = False
                if body.get("mime_type") == "application/spreadsheet":
                    is_spreadsheet = True
                elif body.get("document_path"):
                    path_lower = body["document_path"].lower()
                    if path_lower.endswith((".csv", ".xls", ".xlsx")):
                        is_spreadsheet = True

                # Route to appropriate queues from the declarative PipelineDefinition
                features = body.get("features") or []
                pipeline = PipelineDefinition.load()
                target_queues = pipeline.queues_for(
                    is_spreadsheet=is_spreadsheet, features=features
                )
                logger.info(
                    f"Detected {'spreadsheet' if is_spreadsheet else 'full'} pipeline, "
                    f"routing to queues: {target_queues}"
                )
```

Agregar el import en la sección local de imports de `worker.py`:

```python
from pkg.worker_common.pipeline_config import PipelineDefinition
```

- [ ] **Step 4: Correr suite + commit**

Run: `pytest cmd/extraction-worker/tests -v`
Expected: PASS.

```bash
git add cmd/extraction-worker/worker.py cmd/extraction-worker/tests/test_routing.py
git commit -m "refactor(extraction): route via declarative PipelineDefinition"
```

---

## Task 4: Migrar `required_steps` de completion-worker a `steps_for`

**Files:**
- Modify: `cmd/completion-worker/completion_worker.py:90-102` y `:464-492`

**Interfaces:**
- Consumes: `PipelineDefinition.load` (Task 2).
- Produces: `self.pipeline: PipelineDefinition` cargado en `__init__`; `required_steps` derivado de `steps_for` con la MISMA semántica actual (spreadsheet, audio→extraction, feature inferences). Se eliminan `self.default_required_steps`/`self.spreadsheet_required_steps`.

- [ ] **Step 1: Cargar el pipeline en `__init__`**

En `cmd/completion-worker/completion_worker.py`, en `__init__` (donde están :90-102), reemplazar:

```python
        self.default_required_steps = {"extraction", "embeddings", "entities", "metadata"}
        self.spreadsheet_required_steps = {"extraction", "entities"}
```

por:

```python
        self.pipeline = PipelineDefinition.load()
```

y agregar el import en la sección local de imports:

```python
from pkg.worker_common.pipeline_config import PipelineDefinition
```

- [ ] **Step 2: Reemplazar el bloque de `required_steps` en `check_job_completion`**

En `cmd/completion-worker/completion_worker.py:464-492`, reemplazar:

```python
            required_steps = (
                self.spreadsheet_required_steps
                if is_spreadsheet
                else self.default_required_steps.copy()
            )

            # Audio pipeline uses 'audio' step instead of 'extraction'
            if is_audio and "extraction" in required_steps:
                required_steps.discard("extraction")
                required_steps.add("audio")

            # Add inferences if features were requested
            features_json = self.redis_client.get(f"orchestrator:job:{job_id}:features")
            self.logger.debug(f"Job {job_id}: features_json={features_json}")
            if features_json:
                try:
                    features = json.loads(features_json)
                    if "inferences" in features:
                        required_steps.add("inferences")
                        self.logger.info(
                            f"Job {job_id}: added 'inferences' to required_steps"
                        )
                except Exception as e:
                    self.logger.warning(f"Failed to parse features: {e}")
```

por:

```python
            features_json = self.redis_client.get(f"orchestrator:job:{job_id}:features")
            features = []
            if features_json:
                try:
                    features = json.loads(features_json)
                except Exception as e:
                    self.logger.warning(f"Failed to parse features: {e}")

            required_steps = self.pipeline.steps_for(
                is_spreadsheet=is_spreadsheet,
                is_audio=is_audio,
                features=features,
            )
```

- [ ] **Step 3: Correr suite + commit**

Run: `pytest cmd/completion-worker/tests -v`
Expected: PASS (los tests de `check_job_completion`/`finalize` deben seguir pasando; verificar que ninguno referencia `default_required_steps`/`spreadsheet_required_steps`).

```bash
git add cmd/completion-worker/completion_worker.py
git commit -m "refactor(completion): derive required_steps from PipelineDefinition"
```

---

## Task 5: Wiring docker + runbook de drain + docs

**Files:**
- Modify: `deploy/docker/docker-compose.yml`
- Modify: Dockerfiles de extraction-worker y completion-worker (paths a verificar en ejecución)
- Modify: `AGENTS.md`

**Interfaces:**
- Consumes: `configs/pipeline.json`, `PIPELINE_CONFIG_PATH` (default `/app/configs/pipeline.json`).
- Produces: la config llega a los 2 workers en docker; runbook de migración big-bang documentado.

- [ ] **Step 1: Exponer la config en docker**

En `deploy/docker/docker-compose.yml`, para los servicios `extraction-worker` y `completion-worker`, agregar en `environment:`:

```yaml
    - PIPELINE_CONFIG_PATH=/app/configs/pipeline.json
```

En el Dockerfile de cada uno (ubicar `deploy/docker/` o `cmd/<worker>/Dockerfile`), agregar COPY del archivo al directorio de trabajo del worker:

```dockerfile
COPY configs/pipeline.json /app/configs/pipeline.json
```

Nota: verificar en ejecución el contexto de build de cada Dockerfile (si el contexto es la raíz del repo, `configs/pipeline.json` resuelve; si no, ajustar el COPY).

- [ ] **Step 2: Escribir el runbook en `AGENTS.md`**

```markdown
### Migración big-bang del DAG (D4)

El DAG declarativo vive en `configs/pipeline.json` (`PipelineDefinition`,
cargado por `pkg/worker_common/pipeline_config.py`). `pipeline_version` en
`JobMessage` (escape hatch): los workers lo leen pero lo ignoran si vale "v1".

Runbook de migración (big-bang con drain, NO dual-run):
1. Stop admission: no aceptar nuevos `POST /v1/documents` (pausar llamadas / LB).
2. Drain: esperar `ZCard active_jobs == 0` (jobs en vuelo completan; `JobTimeout=60m`
   acota el peor caso). Caveat: `ExpireStuckJobs` solo expira job-level
   `pending`/`processing`/`extracting`.
3. Deploy: subir imágenes nuevas (orchestrator con `pipeline_version`, workers con
   `configs/pipeline.json`).
4. Resume admission y verificar `GET /v1/documents/:id` con un job de prueba
   (spreadsheet + full + features=["inferences"]).
```

- [ ] **Step 3: Actualizar la sección DAG de `AGENTS.md`**

En "### DAG del pipeline (IMPORTANTE)", reemplazar la mención de routing hardcoded por:

```markdown
El DAG **no** vive en el orchestrator Go. Vive en `configs/pipeline.json`
(`PipelineDefinition`): routing fan-out en `cmd/extraction-worker/worker.py`
(vía `PipelineDefinition.queues_for`) y `required_steps` en
`cmd/completion-worker/completion_worker.py` (vía `PipelineDefinition.steps_for`).
`internal/pipeline/` fue eliminado (dead code, 0 callers).
```

- [ ] **Step 4: Verificación final + commit**

Run:
```bash
make test
pytest cmd/completion-worker/tests -v
pytest cmd/extraction-worker/tests -v 2>&1 | tail -20
```
Expected: Go PASS + Python PASS por-worker.

```bash
git add deploy/docker/docker-compose.yml AGENTS.md
git add deploy/docker cmd/extraction-worker/Dockerfile cmd/completion-worker/Dockerfile
git commit -m "docs(deploy): pipeline.json wiring + big-bang drain runbook"
```

---

## Self-Review (plan 4)

- **Spec coverage (D4, spec C:251 + Detalle D4 + Fase 4.1):** "Migrar routing de `extraction-worker.py:1031-1037` + `required_steps` de `completion-worker.py:102-103` a `PipelineDefinition`" → Tasks 3 y 4 (formato JSON según decisión del usuario, sin YAML). "Añadir `pipeline_version` a `JobMessage` en el mismo deploy; los workers lo leen pero lo ignoran si vale `v1`" → Task 1 (Go + reenvío en extraction; completion solo usa el `version` de la config). "Eliminar `ProcessInParallel`/`WaitForCompletion` — dead code, no migrarlos" → ya eliminados en Plan 1. "Big-bang con drain; `JobTimeout=60m` acota; admission control existe" → Task 5 runbook.
- **Decisión del usuario:** JSON/Python dict en vez de YAML → sin deps nuevas (`encoding/json` Go + `json` Py). Split Plan 3/Plan 4 → este plan es solo D4.
- **Placeholder scan:** sin TBD/TODO. Los paths de Dockerfiles se marcan para verificar en ejecución (no son placeholder de diseño, son dependencia del contexto de build).
- **Type consistency:** `queues_for(*, is_spreadsheet, features)` y `steps_for(*, is_spreadsheet, is_audio, features)` idénticos en Task 2 (def) y Tasks 3-4 (uso). `PipelineDefinition.load()` sin args en producción, con path en tests.
- **Criterio de aceptación:** "Permite reutilizar artifacts" y "capacidad de recuperación" vía versionado; comportamientos existentes intactos (tasks de regresión).