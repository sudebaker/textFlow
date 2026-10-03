# Extracción visual para manuscritos — Plan de implementación (MiniCPM-V 4.5)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Fecha:** 2026-10-02
**Estado:** Aprobado por el usuario; pendiente de ejecución.
**Fuente:** `docs/future_plans/textFlow-plan-extraccion-visual-manuscritos-revisado.md` (especificación revisada)
**Goal:** Añadir recuperación visual (Vision OCR) de páginas difíciles/manuscritas al `extraction-worker` con **contrato externo intacto** y **fast path byte-idéntico**.

**Architecture:** Puerta de dos niveles (gate documental → gate por página) encapsulada en el extraction-worker (paquete nuevo `cmd/extraction-worker/vision/`). Servicio FastAPI `vision-ocr` (patrón de `deploy/docker/image-analyzer`): HTTP + cache Redis por SHA256 + semáforo server-side, sobre vLLM sirviendo MiniCPM-V 4.5 (serving externo, precedente `vllm-qwen-2b`). Sin colas RabbitMQ nuevas, sin cambios en workers downstream.

**Tech Stack:** aio_pika/aiohttp (ya en worker), `pypdfium2`+`Pillow` (nuevos, render local), FastAPI+redis+prometheus_client (servicio), vLLM OpenAI-compatible.

## Invariantes duras (spec §3, §4, §28) — verificadas al final de CADA tarea

1. `VISION_OCR_ENABLED=false` → salida **byte-idéntica** al baseline (golden test).
2. Fast path (gate PASS): **no** renderiza páginas, no llama vision-ocr, no escribe provenance.
3. Bytes originales ya presentes — closure lazy, nunca re-descargar.
4. Error de Vision **no** mata el job: keep Docling + provenance `vision_error` (§16).
5. Presupuesto: timeout/página, max páginas, max segundos, cancelación cooperativa, `degraded` (§18-19).
6. Sin colas nuevas; sin tocar `analyze_text`/`chunk_text`/`SourceClassifier`/workers downstream.

## Mapa de fases (spec §29)

```
F0 golden baseline ── ANTES de tocar nada (prerrequisito)
F1 A verificaciones (docling pages / GPU / vLLM / pesos / montaje)
F2 B paquete vision/ (config, renderer, provenance) + Go DeleteJob
F3 C quality gate LOG-ONLY + hook worker  ← desplegar y MEDIR antes de D/E en prod
F4 D servicio vision-ocr (paralelizable con C; NO activar fallback)
F5 E integración slow path (flag OFF por defecto)
F6 F benchmark + calibración + freeze prompt → solo entonces activar
F7 G LightOnOCR (condicionado — backlog) | H hardening + docs
```

---

## FASE 0 — Golden baseline (antes de cualquier cambio)

### Task 0.1: Fixture PDF determinista

**Files:** Create `cmd/extraction-worker/tests/fixtures.py`, Test `cmd/extraction-worker/tests/test_fixtures.py`

**Step 1 (test primero):**

```python
"""Deterministic minimal-PDF builder (no binary fixtures in git)."""
from fixtures import build_pdf  # noqa: F401  (tests add their dir to sys.path)

def test_build_pdf_two_pages():
    pdf = build_pdf(["Hello world page one", "Second page text here"])
    assert pdf[:8] == b"%PDF-1.4"
    assert pdf.rstrip().endswith(b"%%EOF")
    assert b"/Count 2" in pdf
    assert b"Hello world page one" in pdf and b"Second page text here" in pdf
```

**Step 2 (implementación):** `fixtures.py` construye el PDF programáticamente (catálogo 1 0 R, /Pages 2 0 R, pares Page/Contents 3..4 0 R, 5..6 0 R, /Font 7 0 R tipo Helvetica), calcula offsets xref y `startxref` — builder minimal estándar (~40 líneas, streams `BT /F1 24 Tf 72 720 Td (text) Tj ET`).

**Run:** `pytest cmd/extraction-worker/tests/test_fixtures.py -v` → PASS
**Commit:** `test: add deterministic PDF fixture builder for extraction tests`

### Task 0.2: Harness de fakes para golden test

**Files:** Create `cmd/extraction-worker/tests/golden_utils.py`

```python
class FakeRedis:
    def __init__(self): self.writes = []      # (op, key, args...)
    def hset(self, key, *args, **kw): self.writes.append(("hset", key, args))
    def set(self, key, value): self.writes.append(("set", key, value))
    def hget(self, key, field): return None            # job nunca cancelled
    def publish(self, channel, msg): self.writes.append(("publish", channel, msg))

class FakeStore:  # mismos refs que FSStore: "sha256:<sha256(payload)>"
    def __init__(self): self.blobs = {}
    def put(self, data):
        import hashlib; d = hashlib.sha256(data).hexdigest()
        self.blobs[d] = data; return f"sha256:{d}"
    def get(self, ref): return self.blobs.get(ref[len("sha256:"):])

class FakeExchange:
    def __init__(self): self.published = []   # (routing_key, body, kwargs)
    async def publish(self, message, routing_key=None):
        self.published.append((routing_key, message.body, message.kwargs))

class FakeMsg:     # reemplaza aio_pika.Message para capturar el contrato real
    def __init__(self, body=None, **kw): self.body, self.kwargs = body, kw

def normalize(writes, published):
    # canóniza a JSON: bytes→str, no serializables→"<repr>",
    # timestamps NO deterministas (queued_at en worker.py:1135)→"<ts>"
```

**Run:** `pytest cmd/extraction-worker/tests -v` (los fakes se testean junto al golden en 0.3)
**Commit:** `test: add golden-test fakes (redis, store, channel, msg) for extraction`

### Task 0.3: Golden test + captura de baseline (sobre código PRISTINO)

**Files:** Create `cmd/extraction-worker/tests/test_golden_vision_off.py`, Create `cmd/extraction-worker/tests/golden/baseline.json`

**Determinismos a neutralizar ANTES de instanciar el worker** (módulos ya stubbeados con `sys.modules.setdefault(..., MagicMock())` como en `test_metadata.py:16-27`; añadir al stub-list: `"pypdfium2"`, `"PIL"`, `"PIL.Image"`):

```python
monkeypatch.setenv("PIPELINE_CONFIG_PATH", str(PROJECT_ROOT / "configs/pipeline.json"))
# worker.pipeline = PipelineDefinition.load() en __init__
patch("worker.analyze_text", lambda t: {"language": "es", "char_count": len(t)})  # langdetect/textstat son stubs
patch("worker.tokenizer", None)          # chunking determinista por chars
patch("worker.extract_document_metadata", async lambda *a, **k: {...fijo...})
```

⚠️ `extract_document_metadata` y `analyze_text` se parchean enteras (exiftool/langdetect/devuelven MagicMock: rompen `json.dumps`). `time.time()` en `queued_at` se normaliza con `normalize()`.

**Procedimiento del test:** construir `ExtractionWorker()` → inyectar fakes (`worker.redis_client/event_bus/...`) → `patch(worker.STORE, FakeStore())` → `patch(worker.ExtractionWorker._docling_convert_async)` devolviendo respuesta Docling realista (2 páginas, `md_content` con texto) → `patch(worker.aio_pika.Message, FakeMsg)` → body = `{"job_id": "golden", "document_base64": b64(build_pdf([...]))}` (rama base64, la más simple) → `asyncio.run(worker._process_message_async(msg, FakeExchange))` → `normalize()` de TODAS las writes Redis + published.

**Modo regeneración:** `GOLDEN_REGENERATE=1 pytest ...` reescribe `tests/golden/baseline.json`; ejecución normal compara exactitud.

**Run:**
```bash
GOLDEN_REGENERATE=1 pytest cmd/extraction-worker/tests/test_golden_vision_off.py -v   # captura, UNA vez
pytest cmd/extraction-worker/tests/test_golden_vision_off.py -v                       # PASS
pytest cmd/extraction-worker/tests -v                                                   # todo verde
```
**Commit:** `test: golden regression harness — VISION off must be byte-identical`

---

## FASE A — Verificaciones previas (spec §6; sin código de producto)

### Task A.1: Campos por página en docling-serve 0.12 → decisión `PAGE_TEXT_MODE`

**Run** (docling mapeado a localhost:8000 en compose):

```bash
cd deploy/docker && docker compose up -d docling
TID=$(curl -s -F "files=@/tmp/two_pages.pdf" -F "do_ocr=false" -F "image_export_mode=placeholder" \
  http://localhost:8000/v1/convert/file/async | python -c 'import sys,json;print(json.load(sys.stdin)["task_id"])')
curl -s "http://localhost:8000/v1/status/poll/$TID?wait=30" >/dev/null
curl -s "http://localhost:8000/v1/result/$TID" | jq '.' > /tmp/docling_result.json
jq '.document | keys' /tmp/docling_result.json
jq '.document.pages | length, .[0]' /tmp/docling_result.json
jq '[.document.texts[]? | {text, prov: .prov[0].page_no}]' /tmp/docling_result.json
```

**Decisión a registrar:** si `.document.texts[].prov[].page_no` existe → `PAGE_TEXT_MODE="texts_prov"` (texto por página agrupando items por `page_no`). Si no → `"none"` (gate por página marca todas SUSPECT en documentos SUSPECT; presupuesto/degraded según Task E.2).
**Files:** Create `docs/extraccion-visual-verificacion-A.md` (evidencia A.1–A.4)
**Commit:** `docs: record docling-serve 0.12 per-page fields verification`

### Task A.2: Inventario GPU/VRAM

```bash
nvidia-smi --query-gpu=index,name,memory.total,memory.used --format=csv
curl -s http://localhost:9090/metrics | grep -E 'gpu_(memory|utilization)'
```

Registrar: GPUs, VRAM libre, modelos residentes (bge-m3, GLiNER, docling, whisper, image-analyzer), política de asignación. Con 1 GPU limitada → decidir AWQ vs concurrencia=1..2 vs GPU dedicada (spec §21: decidir por prueba real de serving, no por memoria teórica).
**Files:** append `docs/extraccion-visual-verificacion-A.md`
**Commit:** `docs: record GPU/VRAM inventory for vision-ocr planning`

### Task A.3: vLLM pinneado + MiniCPM-V 4.5 + pesos offline

**Backend de prueba disponible (dev, 2026-10-03):** Ollama en el mac-mini `192.168.88.12:11434` sirve `minicpm-v4.5:latest` (verificado vía `/api/tags`), OpenAI-compatible en `/v1/chat/completions`. Dev/benchmarks de CALIDAD pueden ejecutarse sin GPU: `VISION_LLM_BASE_URL=http://192.168.88.12:11434`, `VISION_LLM_MODEL=minicpm-v4.5:latest`. Los números de VRAM/throughput siguen exigiendo el serving real de vLLM (Fase D).

```bash
pip install vllm==<pin> && python -c "import vllm; print(vllm.__version__)"
vllm serve <local_path_minicpm_v45> --served-model-name minicpm-v-4.5 \
  --max-model-len 4096 --limit-mm-per-prompt image=1 --gpu-memory-utilization 0.85
curl -s http://localhost:8000/v1/chat/completions -d '{"model":"minicpm-v-4.5", \
  "messages":[{"role":"user","content":[{"type":"text","text":"Transcribe the text."},{"type":"image_url","image_url":{"url":"data:image/png;base64,<B64>"}}]}],"temperature":0}'
```

Verificar la matriz de modelos soportados de la versión pinneada para MiniCPM-V 4.5 (repo HF `openbmb/MiniCPM-V-4_5`). Si no soporta → evaluar bump de vLLM aislado para este servicio ANTES de proseguir con D.2. Registrar versión validada + latencia/página + VRAM en serving real.
**Files:** append verificacion-A.md
**Commit:** `docs: record vLLM/MiniCPM-V 4.5 serving validation`

### Task A.4: Mecanismo de montaje air-gap

Trazar cómo `gliner-small-v2.1` pasa de HF cache (`models/huggingface_cache/hub/models--…`) a `${MODELS_PATH}/gliner-small-v2.1` (grep en `deploy/package/*.sh`, `verify-installation.sh`). Replicar ese staging para `models/minicpm-v-4.5/` (read-only).
**Files:** append verificacion-A.md
**Commit:** `docs: record air-gap model staging mechanism for minicpm`

---

## FASE B — Paquete `vision/`: config, renderer, provenance

### Task B.1: requirements + `vision/config.py`

**Files:** Modify `cmd/extraction-worker/requirements.txt` (+`pypdfium2>=4.30`, +`Pillow>=10.0`), Create `cmd/extraction-worker/vision/__init__.py` (vacío), `cmd/extraction-worker/vision/config.py`, Test `cmd/extraction-worker/tests/test_vision_config.py`

```python
"""Env-driven settings for the visual extraction flow (spec §22). os.getenv only."""
import os
from dataclasses import dataclass

def _flag(name, default="false"): return os.getenv(name, default).lower() in ("1", "true", "yes")

@dataclass(frozen=True)
class VisionSettings:
    ocr_enabled: bool; gate_enabled: bool
    ocr_url: str; ocr_timeout: float
    max_pages_per_document: int; max_seconds_per_document: float
    max_concurrency: int; image_dpi: int
    min_chars_per_page: int; image_placeholder_ratio_max: float; garbage_ratio_max: float

    @classmethod
    def load(cls):
        return cls(
            ocr_enabled=_flag("VISION_OCR_ENABLED"),                 # default false — spec §14
            gate_enabled=_flag("VISION_GATE_ENABLED", "true"),       # Phase C log-only
            ocr_url=os.getenv("VISION_OCR_URL", "http://vision-ocr:8080"),
            ocr_timeout=float(os.getenv("VISION_OCR_TIMEOUT", "120")),
            max_pages_per_document=int(os.getenv("VISION_OCR_MAX_PAGES_PER_DOCUMENT", "50")),
            max_seconds_per_document=float(os.getenv("VISION_OCR_MAX_SECONDS_PER_DOCUMENT", "900")),
            max_concurrency=int(os.getenv("VISION_OCR_MAX_CONCURRENCY", "4")),
            image_dpi=int(os.getenv("VISION_OCR_IMAGE_DPI", "200")),
            min_chars_per_page=int(os.getenv("VISION_MIN_CHARS_PER_PAGE", "100")),
            image_placeholder_ratio_max=float(os.getenv("VISION_IMAGE_PLACEHOLDER_RATIO_MAX", "0.3")),
            garbage_ratio_max=float(os.getenv("VISION_GARBAGE_RATIO_MAX", "0.2")),
        )

SETTINGS = VisionSettings.load()   # singleton; tests pasan settings explícitos
```

Test: defaults + parse de flags + override con settings explícitos (sin reload).
**Run:** `pytest cmd/extraction-worker/tests/test_vision_config.py -v`
**Commit:** `feat(extraction): add vision settings module (env-driven, ocr disabled by default)`

### Task B.2: `vision/renderer.py` (materialización por página, spec §7)

**Files:** Create `cmd/extraction-worker/vision/renderer.py`, Test `cmd/extraction-worker/tests/test_vision_renderer.py`

```python
"""PDF page materialization via pypdfium2 — SLOW PATH ONLY (spec §7).
Fast path must never render (golden test enforces)."""
import asyncio, io, logging
logger = logging.getLogger(__name__)

def _pdfium():                       # lazy import: air-gapped test envs may lack it
    import pypdfium2 as pdfium; return pdfium

def count_pages(pdf_bytes: bytes) -> int:
    doc = _pdfium().PdfDocument(pdf_bytes)
    try: return len(doc)
    finally: doc.close()

def render_page(pdf_bytes: bytes, page_number: int, *, dpi: int = 200) -> bytes:
    """Render 1-indexed page to PNG bytes. scale=dpi/72; orientation preserved."""
    doc = _pdfium().PdfDocument(pdf_bytes)
    try:
        bitmap = doc[page_number - 1].render(scale=dpi / 72.0)
        buf = io.BytesIO(); bitmap.to_pil().save(buf, format="PNG")
        return buf.getvalue()
    finally: doc.close()

async def render_page_async(pdf_bytes: bytes, page_number: int, *, dpi: int = 200) -> bytes:
    return await asyncio.to_thread(render_page, pdf_bytes, page_number, dpi=dpi)
```

Test (`pytest.importorskip("pypdfium2")` al inicio): `count_pages(build_pdf([a, b])) == 2`; `render_page` → `\x89PNG`; imagen 300dpi > 120dpi; `page_number=0` y `>n` → `IndexError`; entrada es `bytes` en memoria (sin re-descarga).
**Run:** `pytest cmd/extraction-worker/tests/test_vision_renderer.py -v`
**Commit:** `feat(extraction): add pypdfium2 page renderer (slow path only)`

### Task B.3: `vision/provenance.py` (spec §17: Redis, job-scoped, NO FSStore)

**Files:** Create `cmd/extraction-worker/vision/provenance.py`, Test `cmd/extraction-worker/tests/test_vision_provenance.py`

```python
import json
def write_provenance(redis_client, job_id: str, report: dict) -> None:
    """orchestrator:job:{id}:extraction_provenance — control info, small, job-scoped.
    FSStore stays reserved for large blobs (spec §17)."""
    redis_client.set(f"orchestrator:job:{job_id}:extraction_provenance", json.dumps(report))
```

Test: FakeRedis recibe la clave con JSON serializable (roundtrip `json.loads`).
**Run:** `pytest cmd/extraction-worker/tests/test_vision_provenance.py -v`
**Commit:** `feat(extraction): add per-page extraction provenance writer (redis key)`

### Task B.4: Go — clave de provenance en `DeleteJob`

**Files:** Modify `internal/redis/client.go:640-663` (lista `keys` con `c.key("job", jobID, "extraction_provenance"),`), Test `internal/redis/client_test.go` (en `TestRedisClient_DeleteJob`, línea 252: añadir Set + assert Exists==0 tras delete, patrón de `inference_embeddings` en líneas 267/283).

**Run:** `go test ./internal/redis/... -run 'DeleteJob|KeyNamespacing' -v` y `pytest cmd/extraction-worker/tests -v` (golden verde)
**Commit:** `feat(redis): include extraction_provenance in DeleteJob key list`

---

## FASE C — Quality gate LOG-ONLY (spec §8) + hook

### Task C.1: `vision/gate.py`

**Files:** Create `cmd/extraction-worker/vision/gate.py`, Test `cmd/extraction-worker/tests/test_vision_gate.py`

```python
"""Heuristic quality gate over the existing docling blob (spec §8: log-only first).
Thresholds are INITIAL heuristics — calibrate with real traffic (Phase C metrics)
before enabling fallback (Phase F)."""
import re
from dataclasses import dataclass
from typing import Optional

_IMAGE_PLACEHOLDER_RE = re.compile(r"^\s*!\[[^\]]*\]\([^)]*\)\s*$", re.MULTILINE)
_GARBAGE_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffd]|\(cid:\d+\)")
_NON_PRINTABLE_RE = re.compile(r"[^\w\s.,;:!?()«»\"'¿¡%€$\-–—/\\@#*&+=<>{}\[\]|~^`°]")

@dataclass
class GateSignals:
    page_count: Optional[int]; char_count: int
    chars_per_page: float; text_density: float
    image_placeholder_ratio: float; garbage_ratio: float

def compute_signals(text: str, page_count: Optional[int]) -> GateSignals:
    n = max(page_count or 0, 1)
    char_count = len(text)
    lines = text.splitlines() or [""]
    placeholders = len(_IMAGE_PLACEHOLDER_RE.findall(text))
    garbage = len(_GARBAGE_RE.findall(text))
    non_printable = len(_NON_PRINTABLE_RE.findall(text))
    return GateSignals(
        page_count=page_count, char_count=char_count,
        chars_per_page=char_count / n,
        text_density=1.0 - non_printable / max(char_count, 1),
        image_placeholder_ratio=placeholders / len(lines),
        garbage_ratio=(garbage + non_printable) / max(char_count, 1),
    )

def evaluate_document(s: GateSignals, settings) -> str:   # "pass" | "suspect"
    if s.char_count == 0: return "suspect"
    if s.chars_per_page < settings.min_chars_per_page: return "suspect"
    if s.image_placeholder_ratio > settings.image_placeholder_ratio_max: return "suspect"
    if s.garbage_ratio > settings.garbage_ratio_max: return "suspect"
    return "pass"

def evaluate_page(page_text, settings) -> str:            # "pass" | "suspect"
    if page_text is None: return "suspect"                # branch none (A.1)
    if len(page_text.strip()) < settings.min_chars_per_page: return "suspect"
    return "pass"
```

Test (tabla): texto denso → pass; escaneado sin OCR (vacío) → suspect; líneas placeholder → suspect; `cid:`/`\ufffd` → suspect; `page_text=None` → suspect; página con 20 chars → suspect.
**Run:** `pytest cmd/extraction-worker/tests/test_vision_gate.py -v`
**Commit:** `feat(extraction): add heuristic document/page quality gate`

### Task C.2: `vision/metrics.py`

**Files:** Create `cmd/extraction-worker/vision/metrics.py` (convención `extraction_worker_*`, spec §27)

```python
from prometheus_client import Counter, Histogram
quality_gate_documents_total = Counter("extraction_worker_quality_gate_documents_total",
    "Document-level gate decisions", ["decision"])          # pass|suspect
vision_documents_total = Counter("extraction_worker_vision_documents_total",
    "Documents by path taken", ["path"])                    # fast_path|slow_path
vision_pages_total = Counter("extraction_worker_vision_pages_total",
    "Pages by final backend", ["backend"])                  # docling|vision|vision_error|degraded
vision_page_seconds = Histogram("extraction_worker_vision_page_seconds",
    "Per-page vision processing time (s)")
vision_document_seconds = Histogram("extraction_worker_vision_document_seconds",
    "Slow-path document time (s)")
```

### Task C.3: `vision/fallback.py` — esqueleto log-only + hook en worker

**Files:** Create `cmd/extraction-worker/vision/fallback.py` (versión C), Modify `cmd/extraction-worker/worker.py`

```python
"""Two-level gate + optional Vision OCR fallback (spec §3: contract intact).
run_quality_flow() is the ONLY entry point the worker calls."""
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from .config import VisionSettings, SETTINGS
from .gate import compute_signals, evaluate_document
from .metrics import quality_gate_documents_total, vision_documents_total
logger = logging.getLogger(__name__)

@dataclass
class FallbackContext:
    job_id: str; text: str
    docling_document: dict; page_count: Optional[int]
    get_document_bytes: Callable[[], bytes]      # lazy: solo en slow path
    redis_client: Any
    raise_if_cancelled: Callable[[str], None]
    settings: VisionSettings = field(default_factory=lambda: SETTINGS)

async def run_quality_flow(ctx: FallbackContext) -> str:
    if not (ctx.settings.gate_enabled or ctx.settings.ocr_enabled):
        return ctx.text
    signals = compute_signals(ctx.text, ctx.page_count)
    decision = evaluate_document(signals, ctx.settings)
    quality_gate_documents_total.labels(decision=decision).inc()
    logger.info("quality gate job=%s decision=%s chars/page=%.0f img_ratio=%.2f garbage=%.2f",
                ctx.job_id, decision, signals.chars_per_page,
                signals.image_placeholder_ratio, signals.garbage_ratio)
    if decision == "pass":
        vision_documents_total.labels(path="fast_path").inc()
    # Fase C log-only: SUSPECT solo se registra, no altera nada
    return ctx.text            # byte-idéntico SIEMPRE en esta fase
```

Hook en `worker.py` — tras las 3 ramas de extract (línea ~1041), ANTES de `self._raise_if_cancelled(job_id)` (línea 1044):

```python
# Vision quality flow (spec extraccion-visual §8/§14): with
# VISION_OCR_ENABLED=false this only observes (log+metrics) and returns
# `text` unchanged — golden test enforces byte-identical output.
text = await run_quality_flow(FallbackContext(
    job_id=job_id, text=text,
    docling_document=result.get("docling_document", {}),
    page_count=result.get("docling_pages") if isinstance(result.get("docling_pages"), int) else None,
    get_document_bytes=_get_document_bytes,
    redis_client=self.redis_client,
    raise_if_cancelled=self._raise_if_cancelled))
```

con closure definida dentro de `_process_message_async` (invariante 3 — lazy, sin re-descarga):

```python
def _get_document_bytes() -> bytes:
    if body.get("document_path"):
        with open(body["document_path"], "rb") as f: return f.read()
    if document_bytes_for_meta: return document_bytes_for_meta
    return base64.b64decode(body.get("document_base64", ""))
```

(más el import superior `from vision.fallback import FallbackContext, run_quality_flow`).

Test `test_vision_fallback.py`: log-only devuelve `ctx.text` **exacto** para pass y suspect; counters invocados; **no** toca redis; **no** renderiza (mock assert-not-called); flags off → early return.
**Run:** `pytest cmd/extraction-worker/tests -v` — **GOLDEN DEBE SEGUIR VERDE en este commit**
**Commit:** `feat(extraction): wire log-only quality gate into worker fast path`

### Task C.4: Wiring de configuración

**Files:** Modify `deploy/docker/docker-compose.yml` (env de extraction-worker: `VISION_OCR_ENABLED=${VISION_OCR_ENABLED:-false}`, `VISION_GATE_ENABLED=${VISION_GATE_ENABLED:-true}`, resto `VISION_*` con defaults), Modify `deploy/docker/.env.example` (sección `# VISION OCR (extracción visual)`), Modify `docs/CONFIGURATION.md` (tabla de vars). La línea de Makefile (`test-python` += `deploy/docker/vision-ocr/tests`) se aplica en D.1 cuando el dir exista.

**Run:** `docker compose -f deploy/docker/docker-compose.yml config --quiet`
**Commit:** `chore(deploy): wire vision quality-gate env vars (log-only mode)`

### Task C.5: Despliegue log-only + medición (procedural, spec §8)

Desplegar con tráfico real y registrar en `docs/extraccion-visual-verificacion-A.md`: %pass/suspect, distribución por tamaño/tipo, falsos +/-. Prometheus: `rate(extraction_worker_quality_gate_documents_total[1h])` (job `extraction-worker:8004` ya scrapeado — sin cambios en prometheus.yml). **Criterio de salida de fase:** umbrales calibrados con datos reales antes de activar D/E en producción (reordenamiento crítico §29).

---

## FASE D — Servicio `vision-ocr` (spec §9-§13; paralelizable con C)

### Task D.1: Esqueleto FastAPI + `/transcribe` + `/health`

**Files:** Create `deploy/docker/vision-ocr/app/main.py`, `app/__init__.py`, `requirements.txt` (fastapi, uvicorn[standard], python-multipart, redis, requests, prometheus-client), `Dockerfile` (copiar `deploy/docker/image-analyzer/Dockerfile` — mismo patrón y healthcheck), Test `deploy/docker/vision-ocr/tests/test_main.py` (FastAPI TestClient; env via `monkeypatch` antes de import; `_redis` fake; `requests.post` mockeado)

```python
"""Vision OCR service: transcribes page images via vLLM (MiniCPM-V 4.5).
Pattern: deploy/docker/image-analyzer (spec §9 reuse). Differences:
- NO resize by default (spec §11: MAX_IMAGE_DIM=0 disables; benchmark decides).
- temperature=0 transcription-only prompt (spec §10).
- server-side semaphore (spec §20): GPU protected even against bad clients."""
import asyncio, base64, hashlib, json, logging, os, time
import redis, requests
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from prometheus_client import Counter, Gauge, Histogram, start_http_server

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Backend-agnostic: any OpenAI-compatible server (vLLM prod, Ollama dev).
# Prod default: vllm-minicpm compose service. Dev: mac-mini Ollama
# (192.168.88.12:11434, model minicpm-v4.5:latest) — see A.3 verification.
VISION_LLM_BASE_URL = os.getenv("VISION_LLM_BASE_URL", "http://vllm-minicpm:8000").rstrip("/")
VISION_MODEL = os.getenv("VISION_MODEL", "minicpm-v-4.5")
VISION_TIMEOUT = float(os.getenv("VISION_TIMEOUT", "180"))
VISION_MAX_RETRIES = int(os.getenv("VISION_MAX_RETRIES", "2"))
MAX_CONCURRENCY = int(os.getenv("VISION_MAX_CONCURRENCY", "2"))
QUEUE_WAIT_SECONDS = float(os.getenv("VISION_QUEUE_WAIT_SECONDS", "10"))
MAX_IMAGE_DIM = int(os.getenv("MAX_IMAGE_DIM", "0"))          # 0 = NO resize (spec §11)
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "604800"))
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")

# Spec §10 — transcription, not interpretation. Freeze AFTER benchmark (Fase F).
TRANSCRIBE_PROMPT = (
    "Extract ALL text visible in the image. Transcribe it exactly. "
    "Preserve the original language. Do not translate. Do not summarize. "
    "Do not infer missing text. If text is illegible, leave it empty "
    "rather than inventing content."
)

app = FastAPI(title="textFlow Vision OCR", version="1.0.0")
_redis = redis.from_url(REDIS_URL, decode_responses=True)
_sem = asyncio.Semaphore(MAX_CONCURRENCY)                     # spec §20 server side

vision_requests_total = Counter("vision_ocr_requests_total", "Requests", ["outcome"])
vision_inference_seconds = Histogram("vision_ocr_inference_seconds", "LLM call (s)")
vision_in_flight = Gauge("vision_ocr_in_flight", "In-flight transcriptions")

def _call_vllm(image_bytes: bytes, mime: str) -> str:         # OpenAI-compatible (§9)
    b64 = base64.b64encode(image_bytes).decode()
    payload = {"model": VISION_MODEL, "temperature": 0, "stream": False,
               "messages": [{"role": "user", "content": [
                   {"type": "text", "text": TRANSCRIBE_PROMPT},
                   {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}]}]}
    last = None
    for attempt in range(VISION_MAX_RETRIES):
        try:
            r = requests.post(f"{VISION_LLM_BASE_URL}/v1/chat/completions", json=payload, timeout=VISION_TIMEOUT)
            r.raise_for_status()
            return (r.json()["choices"][0]["message"]["content"] or "").strip()
        except Exception as e:
            last = e; logger.warning("vLLM attempt %d failed: %s", attempt + 1, e)
            time.sleep(min(2 ** attempt, 30))
    vision_requests_total.labels(outcome="error").inc()
    raise HTTPException(status_code=502, detail=f"vLLM call failed: {last}")

@app.post("/transcribe")
async def transcribe(file: UploadFile = File(...), dpi: int = Form(0)) -> dict:
    data = await file.read()
    if not data: raise HTTPException(400, "Empty file")
    mime = file.content_type or "image/png"
    # cache: prompt-aware key (image-analyzer P1 fix pattern, spec §12)
    cache_key = f"vision:{hashlib.sha256(f'{hashlib.sha256(data).hexdigest()}:{TRANSCRIBE_PROMPT}:{VISION_MODEL}:{MAX_IMAGE_DIM}'.encode()).hexdigest()}"
    if (cached := _redis.get(cache_key)):
        vision_requests_total.labels(outcome="cache_hit").inc()
        return json.loads(cached)
    try:    # spec §20: saturated -> 503 + Retry-After (client treats as page error)
        await asyncio.wait_for(_sem.acquire(), timeout=QUEUE_WAIT_SECONDS)
    except Exception:
        vision_requests_total.labels(outcome="saturated").inc()
        raise HTTPException(503, "Saturated", headers={"Retry-After": "5"})
    vision_in_flight.inc(); t0 = time.monotonic()
    try:
        with vision_inference_seconds.time():
            text = _call_vllm(data, mime)
        result = {"extracted_text": text, "model": VISION_MODEL, "cached": False,
                  "dpi": dpi, "elapsed_ms": int((time.monotonic() - t0) * 1000)}
        try: _redis.setex(cache_key, CACHE_TTL_SECONDS, json.dumps(result))
        except Exception as e: logger.warning("cache write failed: %s", e)
        vision_requests_total.labels(outcome="ok").inc()
        return result
    finally:
        vision_in_flight.dec(); _sem.release()

@app.get("/health")
async def health() -> dict:
    return {"status": "healthy", "model": VISION_MODEL, "backend": VISION_LLM_BASE_URL,
            "max_concurrency": MAX_CONCURRENCY, "in_flight": vision_in_flight._value.get()}

start_http_server(int(os.getenv("METRICS_PORT", "9090")))
```

Test: 200 happy-path (mock `requests.post`), 400 vacío, 503 saturado (semáforo a 0 + wait corto), cache hit (2ª llamada no llama vLLM), 502 tras reintentos.
**Run:** `pytest deploy/docker/vision-ocr/tests -v` · `docker compose -f deploy/docker/docker-compose.yml build vision-ocr`
**Commit:** `feat(vision-ocr): add MiniCPM transcription service (cache, semaphore, 503)`

### Task D.2: Compose + GPU overlay + Prometheus + modelos

**Files:** Modify `deploy/docker/docker-compose.yml` (+servicio), Modify `deploy/docker/docker-compose.gpu.yml` (+`vllm-minicpm`), Modify `deploy/prometheus/prometheus.yml` (+job `vision-ocr` → `vision-ocr:9090`, patrón de las líneas 100-107), Modify `deploy/docker/download_models_offline.py`, Modify `Makefile` (`test-python` += `deploy/docker/vision-ocr/tests`), Modify `docs/MODELS.md` + `AGENTS.md` (montaje `models/minicpm-v-4.5/ → vLLM serving`).

```yaml
# docker-compose.yml
  vision-ocr:
    build: { context: ./vision-ocr, dockerfile: Dockerfile }
    container_name: textflow-vision-ocr
    networks: [backend, datastore, docker_default]   # docker_default → vLLM externo
    environment:
    - VISION_LLM_BASE_URL=${VISION_LLM_BASE_URL:-http://vllm-minicpm:8000}
    - VISION_MODEL=${VISION_MODEL:-minicpm-v-4.5}
    - VISION_TIMEOUT=${VISION_TIMEOUT:-180}
    - VISION_MAX_CONCURRENCY=${VISION_MAX_CONCURRENCY:-2}
    - CACHE_TTL_SECONDS=${VISION_CACHE_TTL_SECONDS:-604800}
    - MAX_IMAGE_DIM=0
    - REDIS_URL=${REDIS_URL}
    - PORT=8080
    healthcheck: { test: [CMD, curl, -f, http://localhost:8080/health], interval: 30s, ... }
    restart: unless-stopped
    depends_on: [redis]
```

```yaml
# docker-compose.gpu.yml — serving GPU (runtime nvidia, patrón del overlay).
# Alternativa: backend externo (precedente vllm-qwen-2b) apuntando VISION_LLM_BASE_URL;
# en dev/benchmark ya disponible: Ollama del mac-mini (minicpm-v4.5:latest).
  vllm-minicpm:
    image: vllm/vllm-openai:<pin validado en A.3>
    runtime: nvidia
    volumes:
    - "${MODELS_PATH:?MODELS_PATH is not set}/minicpm-v-4.5:/models/minicpm-v-4.5:ro"
    command: >
      vllm serve /models/minicpm-v-4.5 --served-model-name minicpm-v-4.5
      --max-model-len 4096 --limit-mm-per-prompt image=1
      --gpu-memory-utilization 0.85
    environment: [CUDA_VISIBLE_DEVICES=0, NVIDIA_VISIBLE_DEVICES=0]
    networks: [docker_default]
    healthcheck: { test: [CMD, curl, -f, http://localhost:8000/health], ... }
    restart: unless-stopped
```

`download_models_offline.py`: entrada `openbmb/MiniCPM-V-4_5` en `MODEL_REQUIRED_FILE_GROUPS` (grupos: `config.json`; `tokenizer*`; `preprocessor_config.json|chat_template.json`; `model.safetensors.index.json`; shard `model-00001-of-*.safetensors|model.safetensors`) + entrada en `models` con `critical: True` + staging a `models/minicpm-v-4.5/` según mecanismo A.4.

**Run:** `docker compose -f deploy/docker/docker-compose.yml config --quiet` && `make test-python`
**Commit:** `feat(deploy): add vision-ocr service, vllm-minicpm gpu overlay, model staging`

### Task D.3: Smoke curl-testable (spec §9; acceptance "curl desde el primer día")

```bash
cd deploy/docker && docker compose up -d redis vision-ocr
python -c "from PIL import Image; Image.new('RGB',(800,1200),'white').save('/tmp/p.png')"
curl -s -F file=@/tmp/p.png http://localhost:8080/transcribe | jq
# con vLLM arriba (overlay GPU): repetir con página real renderizada a 200dpi y verificar transcripción
# sin GPU (dev): levantar vision-ocr con VISION_LLM_BASE_URL=http://192.168.88.12:11434 y VISION_MODEL=minicpm-v4.5:latest
```
**Commit:** `docs: vision-ocr curl smoke procedure`

---

## FASE E — Integración slow path (spec §14-§19; flag OFF)

### Task E.1: `vision/client.py`

**Files:** Create `cmd/extraction-worker/vision/client.py`, Test `cmd/extraction-worker/tests/test_vision_client.py`

```python
"""HTTP client for the vision-ocr service (spec §9/§20: client-side limit)."""
import asyncio, logging
import aiohttp
logger = logging.getLogger(__name__)

class VisionSaturatedError(RuntimeError): ...   # 503 -> page falls back to docling
class VisionHTTPError(RuntimeError): ...

class VisionOCRClient:
    def __init__(self, settings):
        self.settings = settings
        self._sem = asyncio.Semaphore(settings.max_concurrency)      # client side (§20)
        self._timeout = aiohttp.ClientTimeout(total=settings.ocr_timeout)  # per page (§18)

    async def transcribe(self, session: aiohttp.ClientSession, image_bytes: bytes, *, dpi: int) -> str:
        async with self._sem:
            form = aiohttp.FormData()
            form.add_field("file", image_bytes, filename="page.png", content_type="image/png")
            form.add_field("dpi", str(dpi))
            async with session.post(f"{self.settings.ocr_url}/transcribe",
                                    data=form, timeout=self._timeout) as resp:
                if resp.status == 503: raise VisionSaturatedError("vision-ocr saturated")
                if resp.status != 200: raise VisionHTTPError(f"vision-ocr HTTP {resp.status}")
                data = await resp.json()
                return data.get("extracted_text") or ""
```

Test (aiohttp stubeado con el patrón del repo): 200 → texto; 503 → `VisionSaturatedError`; 500 → `VisionHTTPError`; semáforo limita concurrencia.
**Run:** `pytest cmd/extraction-worker/tests/test_vision_client.py -v`
**Commit:** `feat(extraction): add vision-ocr http client with client-side semaphore`

### Task E.2: `vision/fallback.py` — slow path completo

**Files:** Modify `cmd/extraction-worker/vision/fallback.py`, Test Modify `cmd/extraction-worker/tests/test_vision_fallback.py`

```python
PAGE_TEXT_MODE = "texts_prov"   # decisión A.1: "texts_prov" | "none"

def extract_page_texts(docling_document: dict, page_count: int) -> list:
    """Per-page docling text. texts_prov: group document.texts by prov[0].page_no
    (join "\\n"). none: [None]*page_count — page gate marks all suspect."""
    if PAGE_TEXT_MODE == "none" or not docling_document:
        return [None] * page_count
    pages = {i: [] for i in range(1, page_count + 1)}
    for item in docling_document.get("texts") or []:
        prov = (item.get("prov") or [{}])[0]
        idx = prov.get("page_no")
        if isinstance(idx, int) and 1 <= idx <= page_count:
            pages[idx].append(item.get("text") or "")
    return ["\n".join(pages[i]).strip() or None for i in range(1, page_count + 1)]

async def run_quality_flow(ctx) -> str:
    # ... rama log-only idéntica a C.3 (gate pass -> vision_documents_total fast_path)
    if decision == "pass" or not ctx.settings.ocr_enabled:
        return ctx.text                       # byte-idéntico (invariantes 1+2)
    try:
        return await _slow_path(ctx, signals, decision)
    except JobCancelledError:
        raise                                  # cancelación coop. propaga (política actual)
    except Exception as e:
        logger.error("vision fallback failed job=%s: %s — keeping docling output", ctx.job_id, e)
        return ctx.text                        # §16: NUNCA mata el job

async def _slow_path(ctx, signals, decision) -> str:
    s = ctx.settings
    ctx.raise_if_cancelled(ctx.job_id)
    pdf_bytes = await asyncio.to_thread(ctx.get_document_bytes)      # lazy, UNA vez (invariante 3)
    if pdf_bytes[:4] != b"%PDF":
        return ctx.text                        # scope §26: PDFs paginados
    page_texts = extract_page_texts(ctx.docling_document, ctx.page_count)
    budget = _Budget(s.max_pages_per_document, s.max_seconds_per_document)
    final, report, any_vision = [], [], False
    client = VisionOCRClient(s)
    async with aiohttp.ClientSession() as session:
        for page_no in range(1, ctx.page_count + 1):
            ctx.raise_if_cancelled(ctx.job_id)                       # §18 entre páginas
            page_text = page_texts[page_no - 1]
            if evaluate_page(page_text, s) == "pass":
                final.append(page_text or "")
                _page_report(report, page_no, "docling", "pass"); continue
            if not budget.allow():
                final.append(page_text or "")                        # §19 conserva disponible
                _page_report(report, page_no, "docling", "degraded", reason="budget_exhausted")
                vision_pages_total.labels(backend="degraded").inc(); continue
            t0 = time.monotonic()
            try:                                                     # §16 try/except ESTRECHO
                image = await render_page_async(pdf_bytes, page_no, dpi=s.image_dpi)
                vision_text = await client.transcribe(session, image, dpi=s.image_dpi)
                vision_page_seconds.observe(time.monotonic() - t0)
                if vision_text.strip():                              # §15 replacement, no fusión
                    final.append(vision_text); any_vision = True
                    _page_report(report, page_no, "vision", "fallback", reason="quality_gate")
                    vision_pages_total.labels(backend="vision").inc()
                else:                                                # vacía: conserva docling
                    final.append(page_text or "")
                    _page_report(report, page_no, "docling", "vision_empty")
                    vision_pages_total.labels(backend="docling").inc()
            except Exception as e:
                vision_page_seconds.observe(time.monotonic() - t0)
                final.append(page_text or "")                        # conserva docling si existe
                _page_report(report, page_no, "vision_error", "vision_error", reason=str(e)[:200])
                vision_pages_total.labels(backend="vision_error").inc()
    assembled = "\n\n".join(final).strip()
    if not assembled:
        assembled = ctx.text          # §16 última defensa: blob docling íntegro
        whole_fallback = True
    if any_vision:                     # sin páginas vision -> texto original byte-exacto
        write_provenance(ctx.redis_client, ctx.job_id,
                         {"gate": {"decision": decision, **asdict(signals)},
                          "pages": report, "vision_pages": budget.used_pages,
                          "duration_s": round(budget.elapsed(), 2),
                          "fallback_whole_document": whole_fallback})
        vision_documents_total.labels(path="slow_path").inc()
        return assembled
    return ctx.text
```

`_Budget.allow()` → `False` cuando `used_pages >= max_pages` o `monotonic() > deadline`; cuenta páginas-vision (no índice de página). Timeout por página = `ocr_timeout` del client (§18).
Test (tabla, FakeRedis + client/renderer mockeados): PASS doc → text sin tocar nada; SUSPECT + vision ok → página reemplazada + provenance; vision error → docling + `vision_error`; budget agotado → `degraded`; todas-fallan → blob docling íntegro; cancelación a mitad → `JobCancelledError` propaga; `PAGE_TEXT_MODE="none"` → todas suspect; check `%PDF`.
**Run:** `pytest cmd/extraction-worker/tests/test_vision_fallback.py -v` && `pytest cmd/extraction-worker/tests -v` — **GOLDEN VERDE (flags default)**
**Commit:** `feat(extraction): full vision fallback — budget, replacement, degraded, provenance`

### Task E.3: Los 3 `extract_text_from_*` devuelven `docling_document`

**Files:** Modify `cmd/extraction-worker/worker.py` (returns en líneas ~793, ~838, ~922: añadir `"docling_document": doc` — el hook de C.3 ya lo consume vía `result.get("docling_document", {})`).

**Run:** `pytest cmd/extraction-worker/tests -v` (golden verde)
**Commit:** `feat(extraction): expose docling document for per-page gate`

### Task E.4: Test de integración flag-ON + wiring final

**Files:** Test Create `cmd/extraction-worker/tests/test_vision_integration.py`, Modify `deploy/docker/docker-compose.yml` (extraction-worker env += `VISION_OCR_URL` + resto `VISION_OCR_*`), `.env.example`, `docs/CONFIGURATION.md`

Test: harness del golden + `VISION_OCR_ENABLED=true` (settings explícitos) + docling response con página casi vacía + client mockeado → Redis recibe `:extraction_provenance`, `:text` con página reemplazada, publish downstream normal (mismo contrato de mensaje).
**Run:** `make test-python` && `make lint` && `go test ./...`
**Commit:** `feat(extraction): integrate vision fallback behind VISION_OCR_ENABLED (default off)`

### Task E.5: Despliegue canario flag OFF (procedural)

Desplegar con `VISION_OCR_ENABLED=false`; verificar golden en producción vía jobs de prueba. La activación se hace SOLO tras Fase F.

---

## FASE F — Benchmark + calibración (spec §23-§24; solo aquí se activa)

- **F.1 Corpus:** 10-30 pág/categoría (manuscrito limpio/difícil, escaneado, PDF imagen, mixto, texto digital, layout complejo) — procedural, fuera de git.
- **F.2 Herramienta:** Create `tools/bench-vision-ocr.py` — renderiza corpus a 120/200/300 DPI → `/transcribe` → CSV (archivo, página, dpi, latencia_ms, chars, tokens, VRAM del resource-manager). Resultados a `docs/extraccion-visual-benchmark.md`.
- **F.3 Calibración + freeze:** actualizar defaults `VISION_MIN_CHARS_PER_PAGE`/ratios con datos de C y F → commit `feat(extraction): calibrate quality-gate thresholds from real traffic`. Congelar `TRANSCRIBE_PROMPT` (§10) → commit. Runbook de activación en `docs/OPERATIONS.md` (drain estilo runbook D4 de AGENTS.md, flip `VISION_OCR_ENABLED=true`, verificación con job de prueba). **La activación en producción es decisión explícita del usuario, no del plan.**
- **F.4 Calidad downstream:** jobs full+`features=["inferences"]` sobre corpus → comparar entidades pre/post (prioridad 1 de métricas §23); CER/WER solo en subconjunto con ground truth.

## FASE G — LightOnOCR (condicionado, §25)

Solo si F muestra brecha de MiniCPM en texto impreso/coordenadas. Backlog — NO construir "porque existe".

## FASE H — Hardening

Revisión completa del checklist §28; alertas Prometheus (`vision_error` rate, `degraded` rate, `in_flight` saturado); OPERATIONS.md (cache `vision:*` TTL + nota GC); ARCHITECTURE.md §pipeline interno.

---

## Aceptación final (spec §28 → verificación)

| Criterio | Verificación |
|---|---|
| flag OFF byte-idéntico + golden automático | Task 0.3 verde en cada commit |
| fast path no renderiza | test C.3 (renderer mock not-called) |
| materialización solo tras SUSPECT doc | test E.2 |
| bytes originales reutilizados | closure lazy `_get_document_bytes` + test |
| sin colas nuevas / HTTP | diff compose: solo `vision-ocr`+`vllm-minicpm` |
| curl-testable + semáforo + offline | D.1/D.2/D.3 (`docker run --network=none`) |
| gate log-only antes de activar | C.5 + reordenamiento §29 |
| error vision no mata job + provenance | tests E.2 (`vision_error`, `degraded`) |
| presupuesto + cancelación | `_Budget` + `raise_if_cancelled` entre páginas |
| GPU inventariada + concurrencia limitada | A.2/A.3 + 2 semáforos (§20) |
| workers downstream intactos | golden (published byte-idéntico) + scope del diff |

**Supuestos (ajustables):** vLLM como servicio GPU del overlay (alternativa: externo tipo `vllm-qwen-2b`); `PAGE_TEXT_MODE` pendiente de A.1; repo HF `openbmb/MiniCPM-V-4_5` + pin de vLLM pendientes de A.3.
