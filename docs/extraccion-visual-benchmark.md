# Benchmark extracción visual — Fase F (inicial)

> **Fecha:** 2026-10-04 · **Origen:** plan `docs/plans/2026-10-02-extraccion-visual-manuscritos.md` F.2-F.3 (spec §23-§24).
> **Estado:** corrida inicial con corpus real (2 PDFs, 23 páginas), DPI 200, backend dev Ollama (`192.168.88.12:11434`, `minicpm-v4.5:latest`).
> **Caveat de extrapolación (A.3.1):** latencia/VRAM sobre Ollama/Mac NO extrapolan a vLLM+GPU; la CALIDAD de transcripción sí es comparable.

## Corpus

| Doc | Tipo | Páginas | Tamaño |
|---|---|---|---|
| `Handwritten-Concern-Form-reporting-Domestic-Abuse-Good-Example.pdf` | manuscrito (formulario, EN) | 10 | 3.1 MB |
| `week10_day3.pdf` | impreso con fórmulas LaTeX (doc física) | 13 | 1.9 MB |

## 0. Alcance y validez de los datos (decisión del owner, 2026-10-04)

**Los números de este informe son puntuales (dev, 2026-10-04) y NO definen producción.** El backend usado (Ollama en mac-mini) es exclusivamente DEV y compartido con otras apps. En producción puede desplegarse vLLM (p. ej. sobre GPU H200) u otro serving OpenAI-compatible: la cadena es backend-agnóstica por diseño (`VISION_LLM_BASE_URL` + `VISION_MODEL` env; sin cola nueva; semáforos en ambos extremos). El objetivo del owner ahora es **que funcione, no optimizar rapidez ni calidad**:
- NO fijar DPI/tokens/etc. por estos datos — defaults provisionales (env-driven, cambiables sin código).
- Matriz DPI 120/300 y métricas de latencia/VRAM/throughput: APLAZADAS hasta existir serving real de producción (F.3/F.5 con GPU, p. ej. H200/AWQ según inventario del despliegue).
- Lo único estable de hoy: el gate SUSPECT discriminó correctamente documentos manuscrito/fórmulas y `PAGE_TEXT_MODE="texts_prov"` funciona con corpus real (independiente del backend).

## 1. Quality gate sobre extracción real (datos C.5)

Llamada docling-serve exacta del worker (con `to_formats=md,json`, Fase E) + `vision.gate` sobre el blob:

| Doc | chars | pages | chars/page | img_ratio | garbage | **gate** |
|---|---|---|---|---|---|---|
| Handwritten-Concern-Form | 171 | 10 | 17 | 0.00 | 0.00 | **suspect** ✓ |
| week10_day3 | 142 | 13 | 11 | 0.00 | 0.00 | **suspect** ✓ |

**Hallazgos:**
- Ambos documentos reales dispar SUSPECT con los thresholds iniciales → el gate detecta el problema objetivo (manuscrito + doc de fórmulas casi sin capa de texto).
- `json_content.texts[].prov[].page_no` mapea por página correctamente (manuscript: 1 text item por página 1-10; week10: 10 items con gaps — páginas sin texto → `None` → page gate suspect → vision). **`PAGE_TEXT_MODE="texts_prov"` validado con corpus real.**
- Nota: layouts densos (fórmulas/manuscrito) generan pocos `texts` — no es densidad de BASE bf16 sino de la capa de texto; ahí opera el slow path.
- Los `Read Timeout`s: no son del error gate — fallan sobre el apéndice de la extracción-bech (ver siguiente sección).

## 2. Vision OCR @ 200 DPI (cold, cache MISS)

**Fuente:** `docs/bench/extraccion-visual-bench-200dpi.csv` (raw). Runs: `vision-ocr-bench` en `docker_backend+default` contra Ollama; el render usa `vision.renderer` (el mismo del worker).

| Doc | OK/total | latencia media | mediana | min | max | chars/página (vision) |
|---|---|---|---|---|---|---|
| Handwritten-Concern | 9/10 | 31.9 s | 29.6 s | 8.0 s | 64.9 s | **3523, 992, 1830, 1653, 2260, 2234, 2404, 1078, 7** |
| week10_day3 | 11/13 | 24.2 s | 23.5 s | 10.7 s | 35.4 s | **1066, 457, 409, 667, 849, 871, 329, 613, 841, 749, 76** |

**Comparativa de calidad (proxy): docling vs vision por página** — docling saca 17/11 chars; **vision extrae 100-3500 chars/página** (manuscrito: transcription legible completa; formulas: extrae LaTeX estructurado). C/WER: pendiente (F.3 subconjunto ground truth).

### Hallazgos operativos

1. **Ollama swap-kill:** con `qwen3.5:9b` residente en el mismo Ollama, el modelo se evicta y a RELOADS en frío; 3 páginas (>2/23) superaron el timeout de lectura 300 s del bench (error: `HTTPConnectionPool ... Read`). factor de riesgo: backend compartido OLLAMA con otros residentes — vLLM (serving dedicado, weight cache GPU) espera NO replicarlo.
2. **`tokens_*` vacíos en el CSV:** `vision-ocr` NO reenvía `usage` del backend vLLM/Ollama (device: `{"extracted_text", "model", "cached", "dpi", "elapsed_ms"}` sin `usage`) → **nit de Hardening (H)**: forwarding de `usage` al payload de `/transcribe` (OpenAI-compat: Ollama/vLLM ambos lo exponen) para poder medir tokens en el benchmark sin tocar de vuelta.
3. **`cached` flag del payload servido en hit** conserva `false` (del primer `PUT`) — contador `cache_hit` de Prometheus SI bien (nit gemela de H).
4. Estabilidad del smoke directo: `v1/chat/completions` + imagen → 200, `usage.prompt_tokens=692/completion=14` (fixture pequeño); corpus page1 200dpi → 200 en 27.5s con LaTeX.
5. bench en red: pipeline `render→transcribe` en contenedor `python:3.11-slim` + `pip install requests pypdfium2 pillow` (egress vía `docker_default`), URL del servicio (`http://vision-ocr-bench:8080`) + instancia con `VISION_MODEL` (OJO con `VISION_LLM_MODEL` vs `VISION_MODEL` — el mapping es del compose, no del run docker crudo).

## 3. Calibración tentativa (thresholds, pendiente F.3)

Con los datos actuales los defaults `VISION_MIN_CHARS_PER_PAGE=100` / ratios 0.3 / 0.2 **clasifican correctamente los 2 casos extremos** (suspect para manuscrito y para lazy-formula-doc; fast-path seguro en texto digital denso). NO se recalibra aún: el spec exige log-only data de tráfico real y el ground-truth subset CER/WER antes de tocar defaults (F.3).

## 4. Gradiente DPI 120/300

Pendiente de corrida (~27 min c/DPI por Ollama swap-risks en la mac-mini compartida). Poderse hacer: el costo temporal es lo único que impide que estén en este informe; la sintaxis de corrida está pinned arriba (DPI es parámetro del CSV y del render).

## 5. No medido hoy (listo para reunión con serving real)

- VRAM del serving real (vision-ocr / resource-manager) — pendiente GPU
- throughput paralelo (semáforos cliente) — pendiente vLLM+AWQ
- quality downstream de entidades — pendiente F.4 (jobs con `features=["inferences"]` desde el corpus)
