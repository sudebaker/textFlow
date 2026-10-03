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
