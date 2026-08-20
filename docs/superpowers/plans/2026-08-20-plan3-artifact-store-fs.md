# Plan 3: Artifact store FS con hash sharding — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Mover los blobs grandes (`:text`, `:chunks`, `:embeddings`, `:inference_embeddings`, `:results`) de Redis a un artifact store FS local con hash sharding (`data/{ab}/{cd}/{hash}.bin`, 65k buckets) detrás de una interfaz `ArtifactStore.Put/Get`, aliviando el `maxmemory 1gb + noeviction`.

**Architecture:** Se crea `pkg/worker_common/artifact_store.py` con `ArtifactStore` (abstract, content-addressed) + `FSStore` (sharding, escritura atómica) + helper `is_artifact_ref`/`resolve`. Cada clave Redis afectada pasa a guardar **solo el ref** `sha256:<64-hex>` en lugar del blob; los readers resuelven el ref contra el store (con fallback legacy: si el valor NO empieza con `sha256:`, se usa tal cual — compat con datos viejos en Redis). `:results` ya se escribe a FS (`completion_worker.py:173-208`) y el orchestrator Go ya lo lee de FS (`cmd/orchestrator/handlers/results.go:90`, `main.go:1239-1241`) → se elimina la escritura duplicada a Redis (`:756-758`). Redis conserva refs cortas, control/locks y `:micro_inferences_raw`.

**Tech Stack:** Python 3.11 stdlib (`hashlib`, `os`, `tempfile`, `abc`, `typing`), msgpack (ya usado), docker-compose, pytest, make test-python.

## Global Constraints

- Air-gapped: no agregar dependencias nuevas. `FSStore` usa solo stdlib.
- Imports Python en 3 secciones: stdlib / third-party / local, orden alfabético (aplica `make format`).
- Nombres: funciones snake_case, constantes UPPER_SNAKE, clases PascalCase.
- No se tocan colas RabbitMQ.
- Se mantiene compat de lectura: un ref en Redis se resuelve contra el store; un valor legacy (no `sha256:`) se usa tal cual (jobs viejos en vuelo durante el deploy).
- `:micro_inferences` y `:micro_inferences_raw` **NO** se migran (spec D3 lista explícitamente solo los 5 keys). `:micro_inferences_raw` queda en Redis por spec.
- No hay TTL en FS: los artifacts no expiran (GC/limpieza fuera de alcance). Se documenta.
- Suites válidas: correr por-worker (`pytest cmd/embeddings-worker/tests -v`, `cmd/completion-worker/tests -v`, etc.). NO correr `make test-python` global (collection errors + prometheus registry pre-existentes).

---

## File Structure

- **Create:** `pkg/worker_common/artifact_store.py` — `ArtifactStore` (ABC), `FSStore`, `STORE` (singleton), `is_artifact_ref`, `resolve`, `resolve_text`.
- **Create:** `cmd/embeddings-worker/tests/test_artifact_store.py` — unit tests del store (se ubica en `cmd/*/tests` porque `make test-python` ejecuta `pytest cmd/*/tests -v`; embeddings-worker es un worker de blobs).
- **Modify:** `cmd/extraction-worker/worker.py` — escribir `:text`/`:chunks` como refs (:973-974).
- **Modify:** `cmd/entities-worker/entities_worker.py` — resolver `:text` (fetch temprano, tras Plan 2) y `:chunks` fallback (:257-259).
- **Modify:** `cmd/metadata-worker/worker.py` — resolver `:text` (:63-69).
- **Modify:** `cmd/embeddings-worker/embeddings_worker.py` — resolver `:chunks` fallback (:95-97); escribir `:embeddings` como ref (:136-140); escribir `:inference_embeddings` como ref (:169-175).
- **Modify:** `cmd/completion-worker/completion_worker.py` — resolver `:text`, `:chunks` en `finalize_job` (:571-588); resolver `:embeddings`/`:inference_embeddings` (:591-592); escribir `:inference_embeddings` generado como ref (:691-693); eliminar `:results` de Redis (:756-758).
- **Modify:** `cmd/completion-worker/tests/test_finalize_job.py` — quitar la aserción sobre el `:results` set.
- **Modify:** `deploy/docker/docker-compose.yml` — volumen `artifacts-data` montado en `/app/data/artifacts` en los 5 workers de blobs.
- **Modify:** `AGENTS.md` — documentar artifact store + keys migradas.

---

## Task 1: Crear `pkg/worker_common/artifact_store.py` + tests

**Files:**
- Create: `pkg/worker_common/artifact_store.py`
- Create: `cmd/embeddings-worker/tests/test_artifact_store.py`

**Interfaces:**
- Consumes: nada externo (solo stdlib).
- Produces:
  - `ARTIFACT_REF_PREFIX = "sha256:"`
  - `class ArtifactStore(ABC)` — `put(data: bytes) -> str`, `get(ref: str) -> Optional[bytes]`.
  - `class FSStore(ArtifactStore)` — `__init__(self, root: str)`; sharding `{root}/{ab}/{cd}/{hash}.bin`.
  - `STORE: ArtifactStore` — singleton desde env `ARTIFACT_PATH` (default `/app/data/artifacts`).
  - `is_artifact_ref(value) -> bool` — True si str/bytes empieza con `sha256:`.
  - `resolve(store: ArtifactStore, value) -> Optional[bytes]` — si es ref, retorna bytes del store; si no, retorna `value` tal cual.
  - `resolve_text(store: ArtifactStore, value) -> Optional[str]` — como `resolve` pero decodifica bytes a `utf-8` con `errors="replace"`; si `value` ya es str, lo retorna.

- [ ] **Step 1: Escribir los tests que fallan — `cmd/embeddings-worker/tests/test_artifact_store.py`**

```python
"""Unit tests for pkg.worker_common.artifact_store."""

import pytest

from pkg.worker_common.artifact_store import (
    ARTIFACT_REF_PREFIX,
    FSStore,
    is_artifact_ref,
    resolve,
    resolve_text,
)


def test_put_returns_prefixed_ref(tmp_path):
    store = FSStore(str(tmp_path))

    ref = store.put(b"hello")

    assert ref.startswith(ARTIFACT_REF_PREFIX)
    assert len(ref) == len(ARTIFACT_REF_PREFIX) + 64


def test_put_get_roundtrip(tmp_path):
    store = FSStore(str(tmp_path))

    ref = store.put(b"hello world")

    assert store.get(ref) == b"hello world"


def test_get_missing_returns_none(tmp_path):
    store = FSStore(str(tmp_path))

    assert store.get(ARTIFACT_REF_PREFIX + "0" * 64) is None


def test_get_non_ref_returns_none(tmp_path):
    store = FSStore(str(tmp_path))

    assert store.get("plain-value") is None


def test_sharding_layout(tmp_path):
    store = FSStore(str(tmp_path))
    ref = store.put(b"x" * 1000)
    digest = ref[len(ARTIFACT_REF_PREFIX):]

    assert (tmp_path / digest[:2] / digest[2:4] / f"{digest}.bin").exists()


def test_content_addressed_deduplicates(tmp_path):
    store = FSStore(str(tmp_path))

    assert store.put(b"same") == store.put(b"same")


def test_put_idempotent(tmp_path):
    store = FSStore(str(tmp_path))
    store.put(b"data")
    store.put(b"data")

    files = list(tmp_path.rglob("*.bin"))
    assert len(files) == 1


def test_is_artifact_ref():
    assert is_artifact_ref(ARTIFACT_REF_PREFIX + "0" * 64) is True
    assert is_artifact_ref("plain text") is False
    assert is_artifact_ref(None) is False
    assert is_artifact_ref(b"sha256:" + b"0" * 64) is True


def test_resolve_ref_and_legacy(tmp_path):
    store = FSStore(str(tmp_path))
    ref = store.put(b"payload")

    assert resolve(store, ref) == b"payload"
    assert resolve(store, "legacy") == "legacy"
    assert resolve(store, b"legacy-bytes") == b"legacy-bytes"
    assert resolve(store, None) is None


def test_resolve_text(tmp_path):
    store = FSStore(str(tmp_path))
    ref = store.put("documento de prueba".encode("utf-8"))

    assert resolve_text(store, ref) == "documento de prueba"
    assert resolve_text(store, "raw text") == "raw text"
    assert resolve_text(store, None) is None
```

- [ ] **Step 2: Correr los tests para verificar que fallan**

Run: `pytest cmd/embeddings-worker/tests/test_artifact_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pkg.worker_common.artifact_store'`

- [ ] **Step 3: Implementar — `pkg/worker_common/artifact_store.py`**

```python
"""Artifact store for textFlow Python workers.

Moves large blobs (text, chunks, embeddings, results) out of Redis into a
local filesystem with content-addressed hash sharding. The abstract
ArtifactStore interface allows a future S3Store without changing callers.
"""

import hashlib
import os
import tempfile
from abc import ABC, abstractmethod
from typing import Optional, Union

ARTIFACT_REF_PREFIX = "sha256:"
DEFAULT_ARTIFACT_PATH = "/app/data/artifacts"


class ArtifactStore(ABC):
    """Content-addressed blob store.

    put() returns a reference string; get() retrieves bytes by reference.
    """

    @abstractmethod
    def put(self, data: bytes) -> str:
        """Store bytes and return a content-addressed ref (sha256:<hex>)."""

    @abstractmethod
    def get(self, ref: str) -> Optional[bytes]:
        """Retrieve bytes by ref; return None if the artifact is missing."""


class FSStore(ArtifactStore):
    """Filesystem store with hash sharding: {root}/{ab}/{cd}/{hash}.bin."""

    def __init__(self, root: str):
        self.root = root

    def _path_for(self, digest: str) -> str:
        return os.path.join(self.root, digest[:2], digest[2:4], f"{digest}.bin")

    def put(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        path = self._path_for(digest)
        if not os.path.exists(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        return f"{ARTIFACT_REF_PREFIX}{digest}"

    def get(self, ref: str) -> Optional[bytes]:
        if not ref.startswith(ARTIFACT_REF_PREFIX):
            return None
        digest = ref[len(ARTIFACT_REF_PREFIX):]
        path = self._path_for(digest)
        try:
            with open(path, "rb") as f:
                return f.read()
        except FileNotFoundError:
            return None


def get_store() -> ArtifactStore:
    """Return the configured ArtifactStore from the ARTIFACT_PATH env var."""
    return FSStore(os.getenv("ARTIFACT_PATH", DEFAULT_ARTIFACT_PATH))


STORE = get_store()


def is_artifact_ref(value: Union[str, bytes, None]) -> bool:
    """Return True if value is an artifact reference (sha256:<hex>)."""
    if isinstance(value, bytes):
        return value.startswith(ARTIFACT_REF_PREFIX.encode())
    return isinstance(value, str) and value.startswith(ARTIFACT_REF_PREFIX)


def resolve(store: ArtifactStore, value: Union[str, bytes, None]) -> Optional[bytes]:
    """Resolve a Redis value that may be an artifact ref.

    If value is an artifact ref, return the stored bytes. Otherwise return
    value unchanged (legacy raw payload) so old data keeps working.
    """
    if is_artifact_ref(value):
        ref = value.decode() if isinstance(value, bytes) else value
        return store.get(ref)
    if isinstance(value, bytes):
        return value
    return value


def resolve_text(store: ArtifactStore, value: Union[str, bytes, None]) -> Optional[str]:
    """Resolve a Redis value that may be an artifact ref into text.

    Returns None when value is None; decodes stored bytes as utf-8 with
    errors="replace"; returns str values unchanged (legacy payloads).
    """
    data = resolve(store, value)
    if data is None:
        return None
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return data
```

- [ ] **Step 4: Correr los tests para verificar que pasan**

Run: `pytest cmd/embeddings-worker/tests/test_artifact_store.py -v`
Expected: PASS (10 passed)

- [ ] **Step 5: Commit**

```bash
git add pkg/worker_common/artifact_store.py cmd/embeddings-worker/tests/test_artifact_store.py
git commit -m "feat(pkg): add ArtifactStore FS backend with hash sharding"
```

---

## Task 2: Montar volumen `artifacts-data` en docker-compose

**Files:**
- Modify: `deploy/docker/docker-compose.yml`

**Interfaces:**
- Consumes: `ARTIFACT_PATH` default `/app/data/artifacts` (Task 1).
- Produces: volumen `artifacts-data` montado en `/app/data/artifacts` en extraction-worker, entities-worker, metadata-worker, embeddings-worker, completion-worker.

- [ ] **Step 1: Agregar el volumen en la sección `volumes:`**

En `deploy/docker/docker-compose.yml`, sección `volumes:` (junto a `redis-data`, `results-data`, ~:597-608):

```yaml
  artifacts-data:
```

- [ ] **Step 2: Montar el volumen en los 5 workers de blobs**

En cada servicio (extraction-worker, entities-worker, metadata-worker, embeddings-worker, completion-worker) agregar dentro de su bloque `volumes:` (junto a los mounts existentes):

```yaml
    - artifacts-data:/app/data/artifacts
```

Nota: no se agrega env `ARTIFACT_PATH` — el default de `get_store()` ya es `/app/data/artifacts`, que coincide con el mount.

- [ ] **Step 3: Verificar**

Run: `grep -n "artifacts-data" deploy/docker/docker-compose.yml`
Expected: 6 ocurrencias (1 en `volumes:` + 5 mounts de servicio).

- [ ] **Step 4: Commit**

```bash
git add deploy/docker/docker-compose.yml
git commit -m "chore(deploy): mount artifacts-data volume on blob workers"
```

---

## Task 3: Migrar `:text` a artifact store (writer + 3 readers)

**Files:**
- Modify: `cmd/extraction-worker/worker.py:973`
- Modify: `cmd/entities-worker/entities_worker.py` (fetch temprano de `:text`)
- Modify: `cmd/metadata-worker/worker.py:63-69`
- Modify: `cmd/completion-worker/completion_worker.py:571-588` (+ `:602`)

**Interfaces:**
- Consumes: `STORE`, `resolve_text` (Task 1).
- Produces: `:text` en Redis guarda el ref `sha256:<hex>`; todos los readers resuelven. Compat legacy: si el valor no es ref, se usa tal cual.

- [ ] **Step 1: Escribir test de regresión — agregar a `test_finalize_job.py`**

Antes de editar, verificar que `test_finalize_job.py` construye un `CompletionWorker` con `redis_client` mockeado. Agregar un test que verifica que `finalize_job` resuelve un `:text` que es ref:

```python
def test_finalize_text_resolves_artifact_ref(worker, tmp_path):
    """finalize_job() resolves a sha256 ref in :text via the artifact store."""
    from pkg.worker_common.artifact_store import FSStore

    store = FSStore(str(tmp_path))
    ref = store.put("documento de prueba".encode("utf-8"))
    # worker uses the default STORE singleton; monkeypatch the module attribute
    import pkg.worker_common.artifact_store as as_module
    as_module.STORE = store

    # redis_client.get returns the ref for :text (other keys return None)
    # (ajustar según el mock existente del worker/fixture)
```

Nota: este test depende del fixture `worker` de `test_finalize_job.py`; si el fixture no expone `redis_client.get` fácil, en su lugar cubrir el resolve vía `test_artifact_store.py` (ya cubre `resolve_text`) y validar la integración en el siguiente paso con el test de embeddings. **Regla:** no romper los 6 tests existentes de dedup; el resolve de texto queda cubierto por `test_resolve_text` (Task 1) + el paso de integración abajo.

- [ ] **Step 2: Escribir en extraction-worker — `:text` como ref**

En `cmd/extraction-worker/worker.py:973`, reemplazar:

```python
                self.redis_client.set(f"orchestrator:job:{job_id}:text", text)
```

por:

```python
                from pkg.worker_common.artifact_store import STORE

                text_ref = STORE.put(text.encode("utf-8"))
                self.redis_client.set(f"orchestrator:job:{job_id}:text", text_ref)
```

(El import se coloca en la sección local de imports del archivo, no inline; ajustar con `make format`.)

- [ ] **Step 3: Resolver en entities-worker — fetch temprano de `:text`**

En `cmd/entities-worker/entities_worker.py`, dentro de `process_message` (bloque que en el Plan 2 quedó así):

```python
        try:
            text = self.redis_client.get(f"orchestrator:job:{job_id}:text")
        except Exception as e:
            self.logger.warning(f"Failed to read document text: {e}")
            text = None
```

reemplazarlo por:

```python
        from pkg.worker_common.artifact_store import STORE, resolve_text

        try:
            text = resolve_text(
                STORE, self.redis_client.get(f"orchestrator:job:{job_id}:text")
            )
        except Exception as e:
            self.logger.warning(f"Failed to read document text: {e}")
            text = None
```

(Imports al inicio del archivo, sección local, orden alfabético.)

- [ ] **Step 4: Resolver en metadata-worker — `:text`**

En `cmd/metadata-worker/worker.py:63-67`, reemplazar:

```python
        text_key = f"orchestrator:job:{job_id}:text"
        text_data = self.redis_client.get(text_key)

        if not text_data:
            raise ValueError(f"No text found in Redis for job: {job_id}")
```

por:

```python
        from pkg.worker_common.artifact_store import STORE, resolve_text

        text_key = f"orchestrator:job:{job_id}:text"
        text_data = resolve_text(STORE, self.redis_client.get(text_key))

        if not text_data:
            raise ValueError(f"No text found in Redis for job: {job_id}")
```

- [ ] **Step 5: Resolver en completion-worker — `:text` en `finalize_job`**

En `cmd/completion-worker/completion_worker.py:602`:

```python
            text = text or ""
```

reemplazar por:

```python
            text = resolve_text(STORE, text) or ""
```

Agregar al inicio de `finalize_job` (o en los imports del archivo):

```python
        from pkg.worker_common.artifact_store import STORE, resolve_text
```

- [ ] **Step 6: Correr suites de los workers afectados**

Run:
```bash
pytest cmd/embeddings-worker/tests -v
pytest cmd/entities-worker/tests -v
pytest cmd/completion-worker/tests -v
pytest cmd/metadata-worker/tests -v 2>&1 | tail -20
```
Expected: PASS en las suites que existen. Si una suite de metadata-worker no existe, anotarlo y seguir (sin crear tests nuevos fuera del alcance).

- [ ] **Step 7: Commit**

```bash
git add cmd/extraction-worker/worker.py cmd/entities-worker/entities_worker.py cmd/metadata-worker/worker.py cmd/completion-worker/completion_worker.py
git commit -m "perf(workers): move job text blob from Redis to artifact store"
```

---

## Task 4: Migrar `:chunks` y eliminar `:results` duplicado

**Files:**
- Modify: `cmd/extraction-worker/worker.py:974`
- Modify: `cmd/entities-worker/entities_worker.py:257-259`
- Modify: `cmd/embeddings-worker/embeddings_worker.py:95-97`
- Modify: `cmd/completion-worker/completion_worker.py` (`:574`, `:610`) + eliminar `:results` (`:756-758`)
- Modify: `cmd/completion-worker/tests/test_finalize_job.py`

**Interfaces:**
- Consumes: `STORE`, `resolve_text` (Task 1).
- Produces: `:chunks` en Redis guarda el ref; `:results` deja de escribirse a Redis (el orchestrator Go ya lee el FS: `handlers/results.go:90`, `main.go:1239-1241`).

- [ ] **Step 1: Escribir en extraction-worker — `:chunks` como ref**

En `cmd/extraction-worker/worker.py:974`, reemplazar:

```python
                self.redis_client.set(f"orchestrator:job:{job_id}:chunks", json.dumps(chunks))
```

por:

```python
                chunks_ref = STORE.put(json.dumps(chunks).encode("utf-8"))
                self.redis_client.set(f"orchestrator:job:{job_id}:chunks", chunks_ref)
```

(`STORE` ya importado en Task 3, Step 2.)

- [ ] **Step 2: Resolver en entities-worker — fallback `:chunks`**

En `cmd/entities-worker/entities_worker.py:256-263`, reemplazar:

```python
        if not chunks:
            chunks_json = self.redis_client.get(f"orchestrator:job:{job_id}:chunks")
            if chunks_json:
                chunks = json.loads(chunks_json)
            else:
```

por:

```python
        if not chunks:
            chunks_json = resolve_text(
                STORE, self.redis_client.get(f"orchestrator:job:{job_id}:chunks")
            )
            if chunks_json:
                chunks = json.loads(chunks_json)
            else:
```

- [ ] **Step 3: Resolver en embeddings-worker — fallback `:chunks`**

En `cmd/embeddings-worker/embeddings_worker.py:94-97`, reemplazar:

```python
        if not chunks:
            chunks_json = self.redis_client.get(f"orchestrator:job:{job_id}:chunks")
            if chunks_json:
                chunks = json.loads(chunks_json)
            else:
```

por:

```python
        if not chunks:
            from pkg.worker_common.artifact_store import STORE, resolve_text

            chunks_json = resolve_text(
                STORE, self.redis_client.get(f"orchestrator:job:{job_id}:chunks")
            )
            if chunks_json:
                chunks = json.loads(chunks_json)
            else:
```

(El import se coloca en la sección local de imports, no inline.)

- [ ] **Step 4: Resolver en completion-worker — `:chunks` en `finalize_job`**

En `cmd/completion-worker/completion_worker.py:610`:

```python
            chunks = json.loads(chunks_json) if chunks_json else []
```

reemplazar por:

```python
            chunks_json = resolve_text(STORE, chunks_json)
            chunks = json.loads(chunks_json) if chunks_json else []
```

- [ ] **Step 5: Eliminar el `:results` duplicado en Redis**

En `cmd/completion-worker/completion_worker.py:756-759`:

```python
            self.redis_client.set(
                f"orchestrator:job:{job_id}:results",
                json.dumps(results, ensure_ascii=False),
            )
```

eliminar el bloque completo (el FS ya se escribe en `save_results_to_file` en `:769`). Verificar que nada más en el repo lee `:results` de Redis (verificado: solo `test_finalize_job.py` lo asocia).

- [ ] **Step 6: Actualizar `test_finalize_job.py`**

En `cmd/completion-worker/tests/test_finalize_job.py`, localizar la aserción que busca el `set` con key `:results` (helper en `:12-15` y su uso) y removerla/ajustarla para que el test verifique el comportamiento restante (por ejemplo, que `save_results_to_file` fue llamado o que el estado quedó `completed`), sin dejar referencias a `:results`.

- [ ] **Step 7: Correr suites**

Run:
```bash
pytest cmd/completion-worker/tests -v
pytest cmd/entities-worker/tests -v
pytest cmd/embeddings-worker/tests -v
```
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add cmd/extraction-worker/worker.py cmd/entities-worker/entities_worker.py cmd/embeddings-worker/embeddings_worker.py cmd/completion-worker/completion_worker.py cmd/completion-worker/tests/test_finalize_job.py
git commit -m "perf(workers): move chunks blob to artifact store; drop duplicate :results"
```

---

## Task 5: Migrar `:embeddings` y `:inference_embeddings` (MsgPack binario)

**Files:**
- Modify: `cmd/embeddings-worker/embeddings_worker.py:136-140` y `:169-175`
- Modify: `cmd/completion-worker/completion_worker.py:591-592` y `:691-693`

**Interfaces:**
- Consumes: `STORE`, `resolve` (Task 1).
- Produces: `:embeddings` y `:inference_embeddings` guardan el ref (`str`) a través de `redis_client`/`redis_raw`; los readers resuelven a bytes antes de `msgpack.unpackb`.

- [ ] **Step 1: Escribir en embeddings-worker — `:embeddings` como ref**

En `cmd/embeddings-worker/embeddings_worker.py:136-140`:

```python
        embeddings_key = f"orchestrator:job:{job_id}:embeddings"
        self.redis_client.set(
            embeddings_key,
            msgpack.packb(embeddings_dict, use_bin_type=True)
        )
```

reemplazar por:

```python
        embeddings_key = f"orchestrator:job:{job_id}:embeddings"
        embeddings_ref = STORE.put(msgpack.packb(embeddings_dict, use_bin_type=True))
        self.redis_client.set(embeddings_key, embeddings_ref)
```

- [ ] **Step 2: Escribir en embeddings-worker — `:inference_embeddings` como ref**

En `cmd/embeddings-worker/embeddings_worker.py:169-175`:

```python
                    if inference_embeddings:
                        packed = msgpack.packb(inference_embeddings, use_bin_type=True)
                        ie_key = f"orchestrator:job:{job_id}:inference_embeddings"
                        pipe = self.redis_client.pipeline()
                        pipe.set(ie_key, packed)
                        pipe.expire(ie_key, 86400)
                        pipe.execute()
```

reemplazar por:

```python
                    if inference_embeddings:
                        ie_key = f"orchestrator:job:{job_id}:inference_embeddings"
                        ie_ref = STORE.put(msgpack.packb(inference_embeddings, use_bin_type=True))
                        self.redis_client.set(ie_key, ie_ref)
```

Nota: se elimina el `expire 86400` (los artifacts en FS no tienen TTL; se documenta en AGENTS.md). Agregar `STORE` a los imports locales de embeddings_worker.py.

- [ ] **Step 3: Resolver en completion-worker — `:embeddings`/`:inference_embeddings`**

En `cmd/completion-worker/completion_worker.py:591-592`:

```python
            embeddings_raw_bytes = self.redis_raw.get(f"orchestrator:job:{job_id}:embeddings")
            inference_embeddings_raw = self.redis_raw.get(f"orchestrator:job:{job_id}:inference_embeddings")
```

reemplazar por:

```python
            embeddings_raw_bytes = resolve(
                STORE, self.redis_raw.get(f"orchestrator:job:{job_id}:embeddings")
            )
            inference_embeddings_raw = resolve(
                STORE, self.redis_raw.get(f"orchestrator:job:{job_id}:inference_embeddings")
            )
```

(`resolve` devuelve `bytes` tanto para ref (del store) como para valor legacy bytes; el `msgpack.unpackb` de `:614` y `:622` queda sin cambios.)

- [ ] **Step 4: Escribir en completion-worker — `:inference_embeddings` generado como ref**

En `cmd/completion-worker/completion_worker.py:691-693`:

```python
                            key = f"orchestrator:job:{job_id}:inference_embeddings"
                            packed = msgpack.packb(inference_embeddings_by_chunk, use_bin_type=True)
                            self.redis_raw.set(key, packed)
```

reemplazar por:

```python
                            key = f"orchestrator:job:{job_id}:inference_embeddings"
                            ie_ref = STORE.put(msgpack.packb(inference_embeddings_by_chunk, use_bin_type=True))
                            self.redis_raw.set(key, ie_ref.encode("utf-8"))
```

- [ ] **Step 5: Correr suites**

Run:
```bash
pytest cmd/embeddings-worker/tests -v
pytest cmd/completion-worker/tests -v
```
Expected: PASS (incluye `test_inference_embeddings.py` que mockea `redis.exists`/`redis.get` sobre `:micro_inferences` — no afectado).

- [ ] **Step 6: Commit**

```bash
git add cmd/embeddings-worker/embeddings_worker.py cmd/completion-worker/completion_worker.py
git commit -m "perf(workers): move msgpack embeddings blobs to artifact store"
```

---

## Task 6: Documentar artifact store en AGENTS.md

**Files:**
- Modify: `AGENTS.md`

- [ ] **Step 1: Agregar sección en AGENTS.md**

```markdown
### Artifact store FS (D3)

Blobs grandes salen de Redis (`maxmemory 1gb + noeviction`) hacia FS local con
hash sharding en `pkg/worker_common/artifact_store.py` (`FSStore`, path
`data/{ab}/{cd}/{sha256}.bin`, 65k buckets, escritura atómica). Keys migradas:
`:text`, `:chunks`, `:embeddings`, `:inference_embeddings`, `:results`. En Redis
quedan solo refs `sha256:<hex>` + control/locks + `:micro_inferences_raw`.
Compat: un valor que NO empieza con `sha256:` se interpreta como payload legacy
(raw) — lectores usan `resolve()`/`resolve_text()`. Sin TTL en FS (limpieza GC
fuera de alcance). Volumen `artifacts-data` montado en `/app/data/artifacts`.
```

- [ ] **Step 2: Commit**

```bash
git add AGENTS.md
git commit -m "docs: document FS artifact store and migrated keys"
```

---

## Self-Review (plan 3)

- **Spec coverage (D3, spec C:250 + Fase 3.1):** "FS local con hash sharding `data/{ab}/{cd}/{hash}.bin`, 65k buckets" → Task 1 (`FSStore._path_for`). "Interfaz `ArtifactStore.Put/Get` (`FSStore` hoy, `S3Store` futuro)" → Task 1 (ABC). "Mover a FS: `:text`, `:chunks`, `:embeddings`, `:inference_embeddings`, `:results`" → Tasks 3, 4, 5. "`:results` ya escribe FS en `completion_worker.py:174-208`" → Task 4 Step 5 elimina el dup. "Dejar en Redis refs cortas + control/locks + `:micro_inferences_raw`" → refs en Tasks 3-5; `:micro_inferences`/`:micro_inferences_raw` no se tocan.
- **Criterio de aceptación:** "Reduce memoria" (blobs fuera del `maxmemory 1gb`) ✓; "Permite reutilizar artifacts" (content-addressed) ✓.
- **Placeholder scan:** sin TBD/TODO.
- **Type consistency:** `STORE`, `resolve`, `resolve_text` se usan con la misma firma en todos los workers; `put(bytes)->str`; `resolve_text(...) -> Optional[str]`. El Step 1 de Task 3 tiene un placeholder explícito ("ajustar según el mock") — es intencional (depende del fixture real de `test_finalize_job.py`) y se resuelve en ejecución revisando el archivo; el coverage de resolve_text ya existe en `test_artifact_store.py`.