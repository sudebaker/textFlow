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


---

## A.3 — vLLM pinneado + MiniCPM-V 4.5 + pesos offline

### COMMANDS RUN

```bash
pip show vllm; python3 -c 'import vllm; print(vllm.__version__)'   # no instalado
curl -s --max-time 3 http://localhost:8000/v1/models               # rc=7 — servicio ausente
curl -s https://huggingface.co/api/models/openbmb/MiniCPM-V-4_5    # OK (red disponible)
curl -sI <repo>/resolve/main/model-0000X-of-00004.safetensors      # tamaños reales
python3 -c "$_MULTIMODAL_REGISTRY" 2>/dev/null                      # sin vllm local → n/a
# por websearch HTTP: docs.vllm.ai stable + latest + tags v0.10.x/v0.11.x (raw.githubusercontent)
curl -s -L https://raw.githubusercontent.com/vllm-project/vllm/<tag>/docs/models/supported_models.md | grep -c MiniCPM-V-4_5
```

### RAW EVIDENCE (recortada)

**Metadatos HF (medidos, 2026-10-03):**
- Repo: `openbmb/MiniCPM-V-4_5` (no gated, apache-2.0, safetensors). Ortografía alternativa `MiniCPM-V_4_5` → error (40x): el ID exacto importa.
- `sha`: `daef484c35ec…`, `lastModified 2026-08-18`.
- Config: `config.json` (architectures: `MiniCPMV`, `auto_map` con remote code: `configuration_minicpm.py`, `modeling_minicpmv.py`), `preprocessor_config.json`, `tokenizer_config.json`, `generation_config.json`, `model.safetensors.index.json`.
- Shards (x-linked-size, en bytes): 5 286 612 176 + 5 301 855 088 + 4 546 851 120 + 2 256 571 800 = **17.39 GB (16.2 GiB)** BF16 en 4 shards.

**vLLM (medidos):**
- No hay vllm instalado en el host (pip 26.2.1 sobre Python 3.14 del sistema — vllm no es instalable ahí) y no hay servicio vLLM en :8000.
- Tabla de modelos soportados (nativa, arquitectura `MiniCPMV`):
  - `v0.10.1` → 0 menciones de `MiniCPM-V-4_5`
  - `v0.10.2`, `v0.11.0`, `v0.11.1`, `v0.11.2` → 1 mención c/u
  - `v0.11.3` → fetch 404 (relación de docs movida/reorganizada; no negativo de soporte)
  - `latest` y `stable` (docs.vllm.ai) → presentes incl. `openbmb/MiniCPM-V-4_5`
- Soporta también `MiniCPM-O`, `MiniCPM-V-4`, `MiniCPM-V-4_6`; patrón modal inputs T+I(Experience)+V.

### FINDING

| Ítem | Estado |
|---|---|
| Repo HF exacto | **Confirmado**: `openbmb/MiniCPM-V-4_5`, safetensors sharded (4 shards), config.json + tokenizer + preprocessor OK. Favorable a download offline sin imagen custom |
| Serve-test real | **BLOCKED**: vllm no instalado (host con Python 3.14, no soportado por vllm) y no hay servicio vLLM vivo; además la VRAM de este host (A.2) no basta ni para BF16 |
| Soporte vLLM >= v0.10.2 | **Confirmado vía docs** (arquitectura `MiniCPMV` nativa) — PERO confirmar-por-docs ≠ serving validado |
| Pin final de vLLM | **Pendiente de validación humana/serving**: pin ≥ `v0.10.2` (primer tag con la mención); validar el pin exacto con serve-test en Fase D. Nota: v0.11.3 reorganiza la doc — no tomar el 404 como señal de no-soporte |

### DECISION

- **Repo HF y staging: confirmados** → D.2 puede usar `openbmb/MiniCPM-V-4_5` con `MODEL_REQUIRED_FILE_GROUPS`: `config.json`, `configuration_minicpm.py`+`modeling_minicpmv.py`+ otros `*.py` (remote code — necesario por `auto_map`), `tokenizer*`, `preprocessor_config.json`, `chat_template.json` (si existe), `model.safetensors.index.json`, `model-0000[1-4]-of-00004.safetensors`.
- **Serve-test de vLLM + MiniCPM-V 4.5: BLOCKED en este host** (sin vllm, Python 3.14, GPU 8 GB). Spec §21: la validación real de serving (version pin + VRAM + latencia/página) se ejecuta en Fase D sobre el host objetivo con GPU. Alguien con GPU ≥24 GB debe: `vllm serve <dir> --served-model-name minicpm-v-4.5 --max-model-len 4096 --limit-mm-per-prompt image=1 --gpu-memory-utilization 0.85` + curl /v1/chat/completions con imagen y registrar ahí versión + VRAM + latencia. **No proseguir D.2 servir hasta ese test.**
- Pin recomendado-con-caveat: `vllm/vllm-openai` en la primera versión estable ≥ `v0.10.2` (arquitectura `MiniCPMV` nativa) — decidir el pin EXACTO con el serve-test de Fase D.


---

## A.3.1 — Backend de prueba (dev) — añidido a petición del usuario (2026-10-03)

**COMMANDS RUN**

```bash
grep -E "LLM_URL|LLM_MODEL" deploy/docker/.env
# LLM_URL=http://192.168.88.12:11434 · LLM_MODEL=ornith:latest
timeout 5 curl -s http://192.168.88.12:11434/api/tags \
  | python3 -c "import sys,json; print([m['name'] for m in json.load(sys.stdin)['models']])"
# ['minicpm-v4.5:latest', 'minicpm5-2b-64k:latest', 'openbmb/minicpm5-2b:latest', ...20 modelos]
```

**FINDING** (medido): Ollama en `192.168.88.12:11434` (mac-mini) sirve **`minicpm-v4.5:latest`**, con API OpenAI-compatible `/v1/chat/completions` con imágenes (mismo patrón que `image-analyzer`). El resto del pipeline ya usa este host/endpoint como LLM de inferencias.

**DECISION**: el servicio `vision-ocr` permanece backend-agnóstico. Las variables del plan se renombran para reflejarlo:
- `VLLM_BASE_URL` → **`VISION_LLM_BASE_URL`** (default prod `http://vllm-minicpm:8000`; dev/bench de calidad → `http://192.168.88.12:11434`).
- `VISION_MODEL` default prod `minicpm-v-4.5`; dev/bench → `minicpm-v4.5:latest` (ojO: id de Ollama ≠ id de vLLM; el env ya lo parametriza).
- Añadir fila a `.env.example` en D.2 cambiando el nombre de las dos variables.
- Aviso al benchmark (F.2): calidad/latencia sobre Ollama/CPU-Mac NO extrapola a VRAM/throughput de vLLM+GPU; comparar solo calidad de transcripción.

---

## A.4 — Mecanismo de montaje air-gap (y staging spec para `models/minicpm-v-4.5/`)

### COMMANDS RUN

```bash
grep -rn "gliner-small-v2.1\|huggingface_cache\|models--" deploy/package/*.sh deploy/docker/*.py
sed -n '95,200p' deploy/package/package.sh
grep -n "MODELS_PATH\|GLINER_MODEL_PATH\|:ro" deploy/docker/docker-compose.yml
cat models/MANIFEST.txt            # no existe en el workspace (ver finding)
ls models/gliner-small-v2.1 models/deberta-v3-small
# Reproduccion en vivo del staging de docling (la unica pieza ejecutada hoy):
docker create quay.io/docling-project/docling-serve:latest
docker cp <cid>:/opt/app-root/src/.cache/docling/models/. models/docling/
```

### RAW EVIDENCE (recortada)

**Cadena actual (trazada):**

1. **Descarga a HF cache** — `deploy/docker/download_models_offline.py`
   - `HF_CACHE_DIR = <root>/models/huggingface_cache` → `hub/models--<org>--<repo>/snapshots/<sha>/` (L99).
   - Modelos: `urchade/gliner_small-v2.1`, `microsoft/deberta-v3-small`, `BAAI/bge-m3` (critical), `Systran/faster-whisper-large-v2` (opcional).
   - Escribe `models/MANIFEST.txt` via `_write_models_manifest()` (lista exit/fail + cadena `Successful`).

2. **Staging a layout de mount** — dos mecanismos segun modelo:
   - GLiNER/bge: `deploy/docker/download-models.py` (script separado, legacy pero vigente): `GLiNER.from_pretrained(...).save_pretrained(models/gliner-small-v2.1)` y `SentenceTransformer(...).save(models/bge-m3)` — directorios top-level con archivos REALES (resueltos, no symlinks del HF cache).
     - Deuda observada (medida): `models/deberta-v3-small/` solo contiene `config.json + spm.model + tokenizer_config.json` (2.4 MB, sin pesos) — el backbone real viaja dentro de `gliner-small-v2.1/pytorch_model.bin`. El staging a top-level NO esta forzado por ningun script ni verify.
   - Docling: `download_models_offline.py:download_docling_models()` copia artefactos desde la imagen Docker con `docker create` + `docker cp` → `models/docling/` (reproducido en vivo hoy contra `:latest`: 740 MB, 4-5 dirs RapidOcr/EasyOcr/etc).

3. **Packaging air-gap** — `deploy/package/package.sh` Step 4: `tar -czf dist/models.tar.gz models/` (todo el dir; tar resuelve symlinks por defecto) + `docker save` de imagenes. `deploy/package/install.sh` extrae el tar (idempotente si `models/` ya existe). `deploy/package/verify-bundle.sh` valida compose + variables + que `models/MANIFEST.txt` contenga `Successful` (L71-75).

4. **Mount en compose** — `${MODELS_PATH}/<dir-top>:/models/<dir-top>[:ro]`:
   - L492 bge-m3 `:ro`; L315 `GLINER_MODEL_PATH=/models/gliner-small-v2.1`; L644 whisper `:ro`; L542 docling artifacts `:ro`.
   - `deploy/package/install.sh:94` extrae `models/` del tar (convencion `models/` en el cwd del instalador; `MODELS_PATH` apunta ahi).

**Medido:** `models/MANIFEST.txt` no existe en el workspace actual (fue generado en una instalacion pasada y no está en git).

### DECISION — staging spec para `models/minicpm-v-4.5/` (para Task D.2; NO implementado aqui)

**Mecanismo elegido: copia de snapshot del HF cache con archivos reales** (patron `download_models_offline.py`), sin usar `download-models.py` (no aplica: no hay wrapper torch para un VLM y `save_pretrained` no aporta nada; vLLM lee un plain HF dir).

1. **Origen:** `models/huggingface_cache/hub/models--openbmb--MiniCPM-V-4_5/snapshots/<sha>/`. Descarga en el host de preparacion con red (via `huggingface_hub.snapshot_download` / `hf download openbmb/MiniCPM-V-4_5 --cache-dir models/huggingface_cache` — HF_HUB_OFFLINE unset solo en ese host). SHA de referencia documentado en A.3: `daef484c35ec…` (2026-08-18).
2. **Destino layout (archivos RESUELTOS: `cp -aL` desde snapshots, NUNCA symlinks):**
   ```
   models/minicpm-v-4.5/
     config.json
     configuration_minicpm.py          # remote code (auto_map) — obligatorio offline
     modeling_*.py                     # todos los *.py del repo (offload/image utils segun repo)
     generation_config.json
     preprocessor_config.json
     tokenizer_config.json             # + tokenizer.model / spm.model, los *.json y *.py extras
     model.safetensors.index.json
     model-0000[1-4]-of-00004.safetensors
   ```
   Incluir TODO el `*.py` del repo HF: con `HF_HUB_OFFLINE=1`, vLLM/transformers cargan el remote code desde disco; si falta un modulo referenciado por `auto_map`, el serve falla en arranque.
3. **Integracion en `download_models_offline.py` (D.2):**
   - `MODEL_REQUIRED_FILE_GROUPS` += grupos: `config.json`; `*.py` (remote code); `tokenizer*`; `preprocessor_config.json`; `chat_template.json` (si existe); `model.safetensors.index.json`; `model-0000[1-4]-of-00004.safetensors`.
   - `models[]` += `{repo_id: openbmb/MiniCPM-V-4_5, type: MiniCPM-V 4.5 (vision OCR), critical: True}`.
   - Nueva funcion `stage_snapshot(repo_id, dest_dir)`: resuelve el snapshot mas reciente del cache → `cp -aL` a `models/minicpm-v-4.5/`; falla si falta `config.json` o el set de shards del index. Idempotente (skip si destino vale con los grupos completos, patron de `download_docling_models`).
   - Presupuesto disco: +~17.4 GB (cache HF) +~17.4 GB (staging) = ~35 GB temporales; opcional `rm -rf hub/models--openbmb--MiniCPM-V-4_5` tras el `cp` exitoso (el staging queda autocontenido en el destino; docling hace lo equivalente al copiar desde imagen).
4. **Mount (solo overlay GPU, D.2):** en el servicio `vllm-minicpm` de `docker-compose.gpu.yml`:
   ```yaml
   volumes:
   - "${MODELS_PATH:?MODELS_PATH is not set}/minicpm-v-4.5:/models/minicpm-v-4.5:ro"
   ```
5. **MANIFEST.txt:** la entrada en `models[]` fluye automaticamente a `_write_models_manifest()` → `verify-bundle.sh` la ancla por la cadena `Successful`. `docs/MODELS.md` (D.2): fila nueva con repo, tamano, mount `:ro`.
6. **Bundle:** `package.sh` sin cambios (el tar de `models/` lo cubre: +~17.4 GB). `verify-bundle.sh/install.sh` sin cambios.

[Ningun fichero de produccion tocado en esta tarea — spec solo.]
