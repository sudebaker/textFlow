# Verificación Fase A — Extracción visual (MiniCPM-V 4.5)

> **Fecha:** 2026-10-03 · **Origen:** `docs/plans/2026-10-02-extraccion-visual-manuscritos.md` (Fase A, Tasks A.1–A.4).
> Tarea de verificación: READ/DIAGNOSE/DOCUMENT — sin cambios de código de producto.
> Entorno de la verificación: host dev `maton` (Manjaro), Docker 29.7.2, con red (para A.3 solo metadatos).

---

## A.1 — Campos por página en docling-serve 0.12 → `PAGE_TEXT_MODE`

### COMMANDS RUN

```bash
# 1. Bring-up infra
docker compose -f deploy/docker/docker-compose.yml up -d docling        # OK tras staging (ver nota)
# 2. PDF de prueba de 2 páginas (builder puro Python del repo)
PYTHONPATH=cmd/extraction-worker/tests python3 -c \
  "from fixtures import build_pdf; open('/tmp/two_pages.pdf','wb').write(build_pdf(['Hello world page one','Second page text here']))"
# 3. Secuencia del worker (--network docker_backend, ver nota de puertos abajo)
curl -F "files=@/tmp/two_pages.pdf" -F "do_ocr=false" -F "image_export_mode=placeholder" \
  http://textflow-docling:5001/v1/convert/file/async        → task_id 3df78583…
curl "http://textflow-docling:5001/v1/status/poll/$TID?wait=30"          → task_status: success
curl "http://textflow-docling:5001/v1/result/$TID"           → /tmp/docling_result.json
# 4. Re-run añadiendo to_formats=json (única forma de obtener el document JSON)
curl -F "files=@/tmp/two_pages.pdf" -F "do_ocr=false" -F "image_export_mode=placeholder" \
     -F "to_formats=md" -F "to_formats=json" ...             → task_id 58038d83…
```

**Notas de infra (medidas):**
- El contenedor arrancó `Exited (3)` en el primer intento: `models/docling/` estaba vacío y la imagen preloadea RapidOCR desde `/opt/app-root/src/.cache/docling/models`. Se resolvió replicando el staging oficial de `download_models_offline.py:download_docling_models()` contra la imagen real en runtime (`quay.io/docling-project/docling-serve:latest`, docker create + docker cp → 740 MB en `models/docling/`, gitignored). Tras eso: `running + healthy`.
- Divulgación de puertos rota en este host SOLO para el contenedor creado por compose: `docker inspect .NetworkSettings.Ports → {"5001/tcp":null}` aunque `HostConfig.PortBindings → 8000→5001` y `docker compose config` muestra la publicación. Un contenedor de prueba con `-p 18000:5001` SÍ publica y escucha en el host (verificado). Causa probable: quirk del daemon 29.7.2 + red compose reutilizada (`docker_backend`). **Consecuencia práctica:** los curl se ejecutaron desde un sidecar `curlimages/curl` en `--network docker_backend` contra `http://textflow-docling:5001`; la imagen `openapi.json` confirma `docling-serve` con el mismo contrato. Servicios como extraction-worker que acceden por la red interna (`http://docling:5001`) no se ven afectados.

### RAW EVIDENCE (recortada)

**Respuesta por defecto (solo md), `document` completo (269 bytes):**

```json
{"document":{"filename":"two_pages.pdf",
 "md_content":"## Hello world page one\n\n## Second page text here",
 "json_content":null,"html_content":null,"text_content":null,"doctags_content":null},
 "status":"success","errors":[],"processing_time":0.947}
```

- Top-level: `document, status, errors, processing_time, timings`.
- **NO existe `document.pages` en la respuesta por defecto.**

**Con `to_formats=md,json` — `json_content` (DoclingDocument completo):**

```
json_content keys: [schema_name, version, name, origin, furniture, body, groups,
                    texts, pictures, tables, key_value_items, form_items, pages]
num pages: 2          # dict indexado por page_no
num texts: 2
```

Item 0 de `texts[]` (verbatim):

```json
{"self_ref": "#/texts/0", "label": "section_header",
 "prov": [{"page_no": 1, "bbox": {"l": 72.0, "t": 737.232, "r": 297.43199…, "b": 715.032,
                  "coord_origin": "BOTTOMLEFT"}, "charspan": [0, 20]}],
 "orig": "Hello world page one", "text": "Hello world page one"}
```

Item 1 (verbatim, recortado):

```json
{"self_ref": "#/texts/1", "label": "section_header",
 "prov": [{"page_no": 2, "bbox": {"l": 72.0, "t": 737.232, "r": 313.488,…}}],
 "orig": "Second page text here", "text": "Second page text here"}
```

Muestra de `pages` (verbatim): `{"size": {"width": 612.0, "height": 792.0}, "image": null, "page_no": 1}`

**Código del worker hoy (solo lectura, `cmd/extraction-worker/worker.py`):**
- `_docling_convert_async` (L657): envía `files, do_ocr, ocr_engine, image_export_mode=placeholder` — **no envía `to_formats`**.
- Los 3 paths de extract (L789/834/918): `"docling_pages": doc.get("pages") and len(doc.get("pages", []))`.
- C.3 del plan asume `result.get("docling_pages")` como page_count de puerta.

### FINDING

| Pregunta | Respuesta (medida) |
|---|---|
| `document.texts[].prov[].page_no` existe | ✅ Sí — pero DENTRO de `document.json_content`, no en el top-level del `document`. Requiere pedir `to_formats=json` |
| `document.pages` (como leído por el worker hoy) | ❌ No existe en la respuesta de docling-serve latest. `doc.get("pages")` → `None` → **`docling_pages` es None HOY en producción con esta imagen y sin `to_formats`** |
| `document.pages` en `json_content` | ✅ Existe como dict `{page_no: {size, image, page_no}}` (sin texto por página — el texto está en `texts[].prov`) |
| Page breaks en `md_content` | Con `md_page_break_placeholder` configurable; insuficiente para delimitación robusta por página |
| Cobertura con OCR/escaneado | ⚠ No verificada en vivo (no hay manuscrito escaneado en el entorno). El pipeline OCR de docling produce items de layout con `prov` igual que el pipeline nativo de texto (mismo DoclingDocument), por lo que la cobertura es plausible; el benchmark de Fase F debe confirmarlo |

### DECISION

**`PAGE_TEXT_MODE = "texts_prov"`**, con condiciones explícitas:

1. **Requisito de implementación:** el worker debe añadir `-F to_formats=md` y `-F to_formats=json` a la llamada de `_docling_convert_async` (paso de Phase E junto a E.3) y leer `document.json_content.texts[].prov[0].page_no` + `document.json_content.pages` para page_count.
2. `extract_page_texts()` agrupa por `prov[0].page_no` tal como el plan E.2 ya esboza; `page_count` debe venir de `len(json_content.pages)` (o `pypdfium2.count_pages` en el GATE del slow path — ambos disponibles; cross-check en E.4).
3. **Caveat registrada:** verificado en vivo SOLO con PDF de texto nativo + `do_ocr=false`. Para manuscritos escaneados (OCR por página) la cobertura se considera plausible pero no probada; Fase F debe validar contra corpus real. El diseño E.2 ya degrada seguro: `page_text=None → suspect`, así que ausencia de `prov` en documentos escaneados solo activaría el slow path, nunca pierde texto.
4. Si alguien valida esto de nuevo tras un bump de imagen de docling-serve, repetir la secuencia del epígrafe COMMANDS RUN.

---

## A.2 — Inventario GPU/VRAM

### COMMANDS RUN

```bash
nvidia-smi --query-gpu=index,name,memory.total,memory.used --format=csv
nvidia-smi --query-gpu=driver_version,compute_cap --format=csv,noheader
curl -s http://localhost:9090/metrics | grep -E 'gpu_'          # resource-manager
docker ps | grep -Ei 'gliner|embeddings|entities|whisper|image|vllm|ollama|docling'
curl http://localhost:11434/api/tags                            # Ollama local
df -h /home | tail -1
grep -n "DEVICE\|CUDA\|runtime" deploy/docker/docker-compose.yml docker-compose.gpu.yml
```

### RAW EVIDENCE (recortada)

```
nvidia-smi:
  0, NVIDIA GeForce RTX 4060, 8188 MiB, 27 MiB, (free 7808 MiB)
  driver 610.57.04, compute_cap 8.9
resource-manager :9090 → rc=7 (down; sin métricas gpu_)
docker ps → SOLO textflow-docling (Up, healthy). Sin embeddings/entities/whisper/image/vllm/ollama containers
pgrep ollama|vllm → nada; :11434 no responde
df -h /home → 229 GB libres
```

Estado declarado en compose (solo lectura):
- Base (`docker-compose.yml`): TODO en CPU (`EMBEDDINGS_DEVICE=cpu`, `ENTITIES_DEVICE=cpu`, `DOCLING_DEVICE=cpu`, `WHISPER_DEVICE=cpu`).
- Overlay (`docker-compose.gpu.yml`): `runtime: nvidia` + `CUDA_VISIBLE_DEVICES=0` para **embeddings-worker, entities-worker, completion-worker, docling (imagen cu128-0.12.0)** → TODOS comparten la GPU 0.
- `models/whisper/` está vacío y las GPUs no residentes: hoy nadie consume VRAM (27 MiB = overhead del driver).

### FINDING

| Dato | Valor | Medido/Supuesto |
|---|---|---|
| GPUs | 1× RTX 4060, 8 GB (Ada, CC 8.9) | **medido** |
| VRAM libre ahora | 7808 MiB / 8188 MiB | **medido** |
| Modelos residentes hoy | ninguno (27 MiB) | **medido** |
| resource-manager metrics | :9090 caído | **medido** |
| Co-residente habitual (overlay GPU) | bge-m3 (~2 GB VRAM) + GLiNER/deberta (~1–2 GB) + docling (~1.5 GB) sobre GPU 0 | **supuesto** (AGENTS.md/overlay; hoy solo CPU) |
| whisper | external-service (deploy/docker/whisper), `models/whisper/` vacío en este host | **medido** (vacío) |
| image-analyzer LLM | `host.docker.internal` vía LLM_BASE_URL (gemma4:e4b en REDIS/llanura dev); no corriendo | **medido** (absente) + `assumed` (pesos) |
| inference-LLM del pipeline | vLLM/Ollama externo, no residente | **medido** (absente) |
| MiniCPM-V 4.5 BF16 | 16.2 GiB (17.39 GB) de pesos — ver A.3 | **medido** (metadatos HF) |

### DECISION (recomendación para D/F — la prueba real de serving §21 va en Fase D)

Una única GPU de 8 GB compartida excluye BF16 de MiniCPM-V 4.5 (16.2 GiB > 8 GB): **no encaja residente, ni siquiera solo**. Opciones bajo UNA GPU:

1. **AWQ/4-bit (recomendado):** ~4–5 GB de pesos (+ KV cache p/`max-model-len` 4096, `limit-mm-per-prompt image=1`) ⇒ ~6–7 GB físicos. Co-residencia con bge-m3/GLiNER/docling es inviable; requiere política de exclusión (docling/GPU workers en CPU base overlay y vLLM dueño de la GPU, o viceversa). Concurrencia del gate: `VISION_MAX_CONCURRENCY=1..2` + semáforo server-side (§20, ya en el plan D.1/D.2).
2. **GPU dedicada:** la única ruta segura para despliegues con volumen alto de páginas; opción hardware, no software.
3. **BF16 entera en 8 GB:** inviable — descartada.

Numeración honesta: los GiB de MiniCPM AWQ son **estimados** (÷4 aprox. + overhead vLLM); el único camino para confirmar es el serve-test real de Fase D (§21), imposible en este host por VRAM.

