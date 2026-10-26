# textFlow — Model Inventory (canonical source)

> **Fuente de verdad.** Este fichero es la referencia canónica del inventario de
> modelos. Todo modelo requerido por textFlow para operar *air-gapped* aparece
> aquí con adquisición y verificación reproducibles. Si un modelo no figura
> aquí, no debe documentarse como requisito del bundle.

Última revisión: 2026-08-28 · commit de referencia: `c6e21b3`.

Regla (§27): `make setup-models` debe producir **todo** el árbol `models/` que
este inventario describe y escribir `models/MANIFEST.txt` (fuente, revision,
sha256). Véase `deploy/docker/download_models_offline.py`.

## Volumen compartido `models-data`

`make setup-models` deja el staging en `models/` (host) + `MANIFEST.txt`. En el host
de despliegue, `make models-sync` copia ese árbol al volumen Docker `models-data`
(servicio one-shot `models-sync`, perfil `models`). Los servicios montan
`models-data:/models:ro` y seleccionan su subdirectorio por env:

| Consumidor | Env | Subdirectorio |
|---|---|---|
| entities-worker | `HF_HOME` / `GLINER_MODEL_PATH` | `huggingface_cache/`, `gliner-small-v2.1/` |
| whisper | `MODEL_PATH` | `whisper/` |
| docling (CPU) | `DOCLING_SERVE_ARTIFACTS_PATH` | `docling/` |
| vllm-minicpm (GPU) | path del comando `vllm serve` | `minicpm-v-4.5/` |

`MODELS_PATH` solo interviene en `make models-sync` (staging → volumen); ya no se
monta en los servicios. `make models-verify` lista tamaños y el manifiesto. Para
modelos obsoletos, recrear el volumen: `docker volume rm docker_models-data`
(proyecto por defecto `docker`) y repetir `make models-sync`.

## Tabla canónica

| # | Componente | Runtime / Servicio | Modelo | Fuente | Ruta local (host) | Ruta montada (contenedor) | Obligatorio | GPU |
|---|------------|-------------------|--------|--------|-------------------|---------------------------|-------------|-----|
| 1 | Embeddings | `embeddings-worker` (Python) | `BAAI/bge-m3` (remoto) | Backend OpenAI-compat `POST /v1/embeddings` | — (sin modelo local) | — | Sí (`EMBEDDINGS_BASE_URL`) | opcional en el backend |
| 2 | NER | `entities-worker` (Python) | `urchade/gliner_small-v2.1` | Hugging Face `urchade/gliner_small-v2.1` | `models/huggingface_cache/hub/models--urchade--gliner_small-v2.1/snapshots/<rev>/` | `/models` (`HF_HUB_OFFLINE=1`) | Sí | opcional (`ENTITIES_DEVICE=cpu\|cuda`) |
| 3 | NER backbone | `entities-worker` (GLiNER) | `microsoft/deberta-v3-small` | Hugging Face `microsoft/deberta-v3-small` | `models/huggingface_cache/hub/models--microsoft--deberta-v3-small/snapshots/<rev>/` | `/models` (`GLINER_BACKBONE_PATH=/models/...`) | Sí | opcional |
| 4 | ASR | `whisper` (`deploy/docker/whisper`) | `large-v2` | Hugging Face `Systran/faster-whisper-large-v2` | `models/huggingface_cache/hub/models--Systran--faster-whisper-large-v2/snapshots/<rev>/` ; `models/whisper/large-v2/` (compat) | `/models/whisper` (`MODEL_PATH=/models/whisper`, `MODEL_SIZE=large-v2`) | solo si audio | opcional (`DEVICE=cpu\|cuda`, `COMPUTE_TYPE=int8\|float16`) |
| 5 | PDF extraction | `docling-serve` | Docling artifacts (layout, table, OCR) | Docling `docling-tools models download` | `models/docling/` (volúmenes por artefacto) | `models-data:/models` (`DOCLING_SERVE_ARTIFACTS_PATH=/models/docling`) | si PDF | ver GPU |
| 5b | Docling image | `docling` (Docker) | `quay.io/docling-project/docling-serve:cu128-0.12.0` | quay.io | `dist/images/docling-serve-cu128-0.12.0.tar.gz` | — | si PDF + GPU | CUDA 12.8 |
| 5c | Docling image (CPU) | `docling` (Docker) | `quay.io/docling-project/docling-serve:latest` | quay.io | `dist/images/docling-serve-latest.tar.gz` + `models/docling/` mount | `models-data:/models:ro` (`DOCLING_SERVE_ARTIFACTS_PATH=/models/docling`) | si PDF + CPU | — |
| 6 | Image analysis | `image-analyzer` (`deploy/docker/image-analyzer`) | modelo vision OCR + modelo descripción | `OCR_BASE_URL`/`OCR_MODEL` y `IMAGE_BASE_URL`/`IMAGE_MODEL` (OpenAI-compat) | — (externo) | — | solo si imagen | sí (vía backend) |
| 7 | Inference (hechos) | `inference-worker` | modelo instruct del backend | `INFERENCE_BASE_URL` + `INFERENCE_MODEL` (OpenAI-compat) | — (externo) | — | opcional (`features=["inferences"]`) | sí (vía backend) |

Notas:

- (4) `large-v2` es el default en `.env.example:WHISPER_MODEL=large-v2` y el que
  espera `deploy/docker/whisper` (`MODEL_SIZE` / `MODEL_PATH`). Otros tamaños
  (`tiny…large-v3`) son intercambiables cambiando la env y descargando el repo
  `Systran/faster-whisper-<size>` correspondiente.
- (5) Dual Docling (acuerdo Fase A A4): **CPU** → artifacts externos
  `models/docling/` montados (`HF_HUB_OFFLINE=1`); **GPU** → imagen CUDA
  `cu128-0.12.0` self-contained (no requiere `models/docling/`). No documentar
  `models/docling/` como requisito si la instalación usa GPU-only.
- (6)(7) Image-analyzer e inference-worker son **consumidores** de un runtime
  LLM externo al bundle textFlow. Para air-gapped provisionar el runtime +
  el modelo por separado (§18–19). Si el backend es Ollama: `ollama pull`,
  `ollama export` / `ollama cp`, `import` offline.

## Ficha por modelo

### 1. BAAI/bge-m3 (remoto)

- Ya **no** se monta en `embeddings-worker` ni en `completion-worker`.
- Backend: servicio OpenAI-compatible `POST /v1/embeddings` en `EMBEDDINGS_BASE_URL` con `EMBEDDINGS_MODEL` (por defecto `BAAI/bge-m3`, 1024 dimensiones).
- Air-gapped: empaquetar y desplegar el serving (vLLM/TEI) por separado; el bundle de textFlow solo contiene la configuración.
- Validación: contract check de 1024 dimensiones contra el endpoint antes del despliegue.

### 2. urchade/gliner_small-v2.1

- Repositorio: `https://huggingface.co/urchade/gliner_small-v2.1`.
- Archivos mínimos: `config.json`, `gliner_config.json`, `tokenizer_config.json`,
  `special_tokens_map.json`, (`spm.model` ∨ `tokenizer.json` ∨ `vocab.txt`),
  (`model.safetensors` ∨ `pytorch_model.bin`).
- Descarga/validación/consumidor análogos a (1) (`GLiNER.from_pretrained`, `entities-worker`).

### 3. microsoft/deberta-v3-small (backbone GLiNER)

- Repositorio: `https://huggingface.co/microsoft/deberta-v3-small`.
- Archivos mínimos: `config.json`, `tokenizer_config.json`, `special_tokens_map.json`,
  (`spm.model` ∨ `tokenizer.json`), (`model.safetensors` ∨ `pytorch_model.bin`).
- Ruta: `models/huggingface_cache/hub/models--microsoft--deberta-v3-small/...`.
- Env: `GLINER_BACKBONE_PATH` / `DEBERTA_MODEL_PATH` → `/models/...` (contenedor). Mapear a la snapshot real o usar `hf_transfer` del `HF_HOME`.
- Descarga/validación: `download_models_offline.py` (no `download-models.py` — éste no descarga DeBERTa).
- Consumidor: `entities-worker` (GLiNER backbone); no documentar `models/deberta-v3-small/` como layout independiente si se usa el cache hub.

### 4. Systran/faster-whisper-large-v2

- Fuente: `https://huggingface.co/Systran/faster-whisper-large-v2` (CTranslate2).
- Archivos mínimos: `config.json`, `model.bin` ∨ `model.safetensors`, `tokenizer.json` ∨ `vocabulary.txt` ∨ `vocab.json`, `preprocessor_config.json`.
- Ruta host: `models/huggingface_cache/hub/models--Systran--faster-whisper-large-v2/...` (+ compat `models/whisper/large-v2/` si se copia).
- Montaje Compose `deploy/docker/docker-compose.yml:whisper`: `models-data:/models:ro` con `MODEL_PATH=/models/whisper`, `MODEL_SIZE=large-v2`.
- Env: `WHISPER_MODEL` (`tiny…large-v3`), `WHISPER_DEVICE=cpu|cuda`, `WHISPER_COMPUTE_TYPE=int8|float16`.
- Descarga: `download_models_offline.py` (repo `Systran/faster-whisper-large-v2`).
- Validación: `WhisperModel("models/whisper/large-v2", local_files_only=True)` offline; `curl -f http://localhost:8080/health`.

### 5. Docling artifacts + imágenes

- Fuente: Docling official `docling-tools models download` o imagen CUDA bundled.
- CPU path: `deploy/docker/download_models_offline.py:download_docling_models` hace `docker create` + `docker cp ...:/opt/app-root/src/.cache/docling/models → models/docling/` (snapshot del contenido de la imagen de referencia). `docker-compose.yml:docling` monta `models-data:/models:ro` y fija `DOCLING_SERVE_ARTIFACTS_PATH=/models/docling`.
- GPU path: `docker-compose.gpu.yml:docling` fija `image: quay.io/docling-project/docling-serve:cu128-0.12.0` (`runtime: nvidia`, `DOCLING_DEVICE=cuda`). No requiere `models/docling/`.
- Validación: `curl -f http://localhost:5001/openapi.json` + E2E `bin/client -i file.pdf -o out.json` (CPU y GPU deben producir mismo `text`, distinto `pages/sec`).
- No documentar `models/docling/` si la instalação es GPU-only (§10). La documentación debe coincidir exactamente con el Compose usado.

### 6. Image-analyzer (vision LLM)

- Servicio: `deploy/docker/image-analyzer` (FastAPI `POST /analyze` OCR + `POST /describe` descripción).
- Env: `OCR_BASE_URL`/`OCR_MODEL` para OCR y `IMAGE_BASE_URL`/`IMAGE_MODEL` para descripción (si faltan, la descripción usa el backend OCR), `MAX_IMAGE_DIM`, `CACHE_TTL_SECONDS`.
- Este LLM es **externo a `models/` de textFlow** (fase A decisión: *externo al bundle*).
- Air-gapped: provisionar por separado:
  - **Ollama** (recomendado doc §18): en máquina online `ollama pull <model>`, exportar `ollama export` / copiar `~/.ollama/models` al bundle (configurar `OLLAMA_MODELS`), en target `ollama import` / copia. Documentar instalación Ollama, `pull`, `export/transfer`, `import`.
  - **vLLM** (prod): empaquetar imagen vLLM + weights del modelo vision; desplegar con `vllm serve`.

### 7. Inference LLM (hechos)

- Servicio: `cmd/inference-worker` consume runtime OpenAI-compatible (`INFERENCE_BASE_URL`, `INFERENCE_MODEL`, `INFERENCE_LLM_TIMEOUT`, `INFERENCE_MAX_CONCURRENCY`, `INFERENCE_WORKER_REPLICAS`).
- Air-gapped igual que (6): externo, no en `models/`. Documentar `vLLM` vs `Ollama` por separado (§19) y cómo provisionar cada uno offline. Fase A: *externo al bundle*.

### 8. openbmb/MiniCPM-V-4_5 (vision OCR)

- Repositorio: `https://huggingface.co/openbmb/MiniCPM-V-4_5`.
- Ruta host / montaje: `models/minicpm-v-4.5/` dentro de `models-data` → `vllm-minicpm` monta `models-data:/models:ro` y sirve `/models/minicpm-v-4.5` (`deploy/docker/docker-compose.gpu.yml`).
- Formato: snapshot HF plano (no cache hub): `config.json`, `tokenizer.json`, `tokenizer_config.json`, `preprocessor_config.json`, `model.safetensors.index.json`, shards `model-00001..00004-of-00004.safetensors` (4 shards, ~17.4 GB, BF16 ≈ 8.7B params, verificado en el repo HF). Existen variantes AWQ cuantizadas de la comunidad como alternativa con menos VRAM; el bundle usa la BF16 original.
- Descarga: `hf download openbmb/MiniCPM-V-4_5 --local-dir models/minicpm-v-4.5/` (o `huggingface-cli download` en máquinas sin `hf`), desde la máquina online de preparación.
- `deploy/docker/download_models_offline.py` lista la entrada (`critical: False`) para descarga/validación con snapshot_download.
- Serving **prod (GPU)**: `vllm/vllm-openai:0.10.2` (pin validado en §21, soporte MiniCPMV >= v0.10.2) vía `docker compose -f deploy/docker/docker-compose.yml -f deploy/docker/docker-compose.gpu.yml up -d vllm-minicpm`; el backend OpenAI-compatible queda en `http://vllm-minicpm:8000/v1`.
- Serving **dev/benchmark (sin GPU)**: Ollama del mac-mini `192.168.88.12:11434`, modelo `minicpm-v4.5:latest` (`ollama pull minicpm-v4.5`); en `.env`: `VISION_LLM_BASE_URL=http://192.168.88.12:11434`, `VISION_LLM_MODEL=minicpm-v4.5:latest`.
- Consumidor: `vision-ocr` (`deploy/docker/vision-ocr`, `POST /transcribe`) vía `VISION_LLM_BASE_URL`/`VISION_LLM_MODEL`. `critical: False` hasta validar VRAM/latencia en serving real (Fase D/D.3) — en dev el modelo vive fuera del bundle (passthrough Ollama).

## Procedimientos canónicos

### Online prep machine

```bash
# 0. Requisitos host: Docker, jq, bash, curl, espacio (~15 GB con large-v2 + docling)
make setup-models
# → deploy/docker/download_models_offline.py
#   HF: gliner, deberta, whisper-large-v2 (bge-m3 opcional solo si self-hosteas el backend de embeddings)
#   Docling artifacts → models/docling/ (vía docker create/cp de la imagen cu128/self)
#   Manifiesto: models/MANIFEST.txt (repo, revisión, sha256, tamaño por archivo)
```

### Verificación offline (§16)

```bash
docker run --network=none entities-worker            # falla si GLiNER/DeBERTa no están
python -c "from faster_whisper import WhisperModel; WhisperModel('models/whisper/large-v2', local_files_only=True)"
curl -f http://localhost:5001/model/list  # Docling artifacts
```

### Bundle

`make package` produce `dist/` (`images/*.tar.gz`, `models.tar.gz`, `config/`, `install.sh`, `lib.sh`, `MANIFEST.txt` con commit, timestamp, Docker version, imágenes+digests, models sha256, tamaño). Ver `deploy/package/package.sh:EXTERNAL_IMAGES` derivado de `docker compose config` (no duplicar `rabbitmq:3.12`).

## Referencias

- Docling GPU/deployment: https://docling-project.github.io/docling/usage/gpu/ y `/usage/api_server/deployment/`
- Docling model prefetch / offline: `docling-tools models download` (ver `deploy/docker/download_models_offline.py`)
- Faster-Whisper offline: `WhisperModel(path, local_files_only=True)` + `MODEL_PATH=/models/whisper`
