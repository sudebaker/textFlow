# textFlow — Model Inventory (canonical source)

> **Fuente de verdad.** Este fichero es la referencia canónica del inventario de
> modelos. Todo modelo requerido por textFlow para operar *air-gapped* aparece
> aquí con adquisición y verificación reproducibles. Si un modelo no figura
> aquí, no debe documentarse como requisito del bundle.

Última revisión: 2026-08-28 · commit de referencia: `c6e21b3`.

Regla (§27): `make setup-models` debe producir **todo** el árbol `models/` que
este inventario describe y escribir `models/MANIFEST.txt` (fuente, revision,
sha256). Véase `deploy/docker/download_models_offline.py`.

## Tabla canónica

| # | Componente | Runtime / Servicio | Modelo | Fuente | Ruta local (host) | Ruta montada (contenedor) | Obligatorio | GPU |
|---|------------|-------------------|--------|--------|-------------------|---------------------------|-------------|-----|
| 1 | Embeddings | `embeddings-worker` (Python) | `BAAI/bge-m3` | Hugging Face `BAAI/bge-m3` | `models/huggingface_cache/hub/models--BAAI--bge-m3/snapshots/<rev>/` | `/models` (`HF_HOME=/models` + `local_files_only=True`) | Sí | opcional (`EMBEDDINGS_DEVICE=cpu\|cuda`) |
| 2 | NER | `entities-worker` (Python) | `urchade/gliner_small-v2.1` | Hugging Face `urchade/gliner_small-v2.1` | `models/huggingface_cache/hub/models--urchade--gliner_small-v2.1/snapshots/<rev>/` | `/models` (`HF_HUB_OFFLINE=1`) | Sí | opcional (`ENTITIES_DEVICE=cpu\|cuda`) |
| 3 | NER backbone | `entities-worker` (GLiNER) | `microsoft/deberta-v3-small` | Hugging Face `microsoft/deberta-v3-small` | `models/huggingface_cache/hub/models--microsoft--deberta-v3-small/snapshots/<rev>/` | `/models` (`GLINER_BACKBONE_PATH=/models/...`) | Sí | opcional |
| 4 | ASR | `whisper` (`deploy/docker/whisper`) | `large-v2` | Hugging Face `Systran/faster-whisper-large-v2` | `models/huggingface_cache/hub/models--Systran--faster-whisper-large-v2/snapshots/<rev>/` ; `models/whisper/large-v2/` (compat) | `/models` (`MODEL_PATH=/models`, `MODEL_SIZE=large-v2`) | solo si audio | opcional (`DEVICE=cpu\|cuda`, `COMPUTE_TYPE=int8\|float16`) |
| 5 | PDF extraction | `docling-serve` | Docling artifacts (layout, table, OCR) | Docling `docling-tools models download` | `models/docling/` (volúmenes por artefacto) | `/opt/app-root/src/.cache/docling/models` (CPU) | si PDF | ver GPU |
| 5b | Docling image | `docling` (Docker) | `quay.io/docling-project/docling-serve:cu128-0.12.0` | quay.io | `dist/images/docling-serve-cu128-0.12.0.tar.gz` | — | si PDF + GPU | CUDA 12.8 |
| 5c | Docling image (CPU) | `docling` (Docker) | `quay.io/docling-project/docling-serve:latest` | quay.io | `dist/images/docling-serve-latest.tar.gz` + `models/docling/` mount | `/opt/app-root/src/.cache/docling/models:ro` | si PDF + CPU | — |
| 6 | Image analysis | `image-analyzer` (`deploy/docker/image-analyzer`) | modelo vision del backend | `LLM_BASE_URL` + `LLM_MODEL` (OpenAI-compat) | `LLM_BASE_URL` (Ollama `http://host:8080` o vLLM) | — | solo si imagen | sí (vía backend) |
| 7 | Inference (hechos) | `inference-worker` | modelo instruct del backend | `LLM_URL` + `LLM_MODEL` (OpenAI-compat) | `LLM_BASE_URL` (Ollama/vLLM) | — | opcional (`features=["inferences"]`) | sí (vía backend) |

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

### 1. BAAI/bge-m3

- Repositorio: `https://huggingface.co/BAAI/bge-m3`.
- Archivos mínimos: `config.json`, `tokenizer_config.json`, `modules.json`,
  (`spm.model` ∨ `tokenizer.json` ∨ `vocab.txt`), (`model.safetensors` ∨ `pytorch_model.bin`).
- Ruta: `models/huggingface_cache/hub/models--BAAI--bge-m3/snapshots/<rev>/`.
- Formato: Hugging Face cache `snapshot_download` (`hub/` + `snapshots/<hash>/`).
- Tamaño: ≈ 2.2 GB.
- Descarga: `python deploy/docker/download_models_offline.py` (`snapshot_download("BAAI/bge-m3", cache_dir=models/huggingface_cache/hub)`).
- Validación: `_is_snapshot_complete` (todos los grupos del `MODEL_REQUIRED_FILE_GROUPS` presentes) + `verify_cache_structure`; offline `local_files_only=True` en `SentenceTransformer`.
- Consumidor: `cmd/embeddings-worker` (`SentenceTransformer("BAAI/bge-m3", cache_folder=...)`).

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
- Montaje Compose `deploy/docker/docker-compose.yml:whisper`: `${MODELS_PATH}/whisper:/models:ro` con `MODEL_PATH=/models`, `MODEL_SIZE=large-v2`.
- Env: `WHISPER_MODEL` (`tiny…large-v3`), `WHISPER_DEVICE=cpu|cuda`, `WHISPER_COMPUTE_TYPE=int8|float16`.
- Descarga: `download_models_offline.py` (repo `Systran/faster-whisper-large-v2`).
- Validación: `WhisperModel("models/whisper/large-v2", local_files_only=True)` offline; `curl -f http://localhost:8080/health`.

### 5. Docling artifacts + imágenes

- Fuente: Docling official `docling-tools models download` o imagen CUDA bundled.
- CPU path: `deploy/docker/download_models_offline.py:download_docling_models` hace `docker create` + `docker cp ...:/opt/app-root/src/.cache/docling/models → models/docling/` (snapshot del contenido de la imagen de referencia). `docker-compose.yml:docling` monta `${MODELS_PATH}/docling:/opt/app-root/src/.cache/docling/models:ro`.
- GPU path: `docker-compose.gpu.yml:docling` fija `image: quay.io/docling-project/docling-serve:cu128-0.12.0` (`runtime: nvidia`, `DOCLING_DEVICE=cuda`). No requiere `models/docling/`.
- Validación: `curl -f http://localhost:5001/openapi.json` + E2E `bin/client -i file.pdf -o out.json` (CPU y GPU deben producir mismo `text`, distinto `pages/sec`).
- No documentar `models/docling/` si la instalação es GPU-only (§10). La documentación debe coincidir exactamente con el Compose usado.

### 6. Image-analyzer (vision LLM)

- Servicio: `deploy/docker/image-analyzer` (FastAPI `POST /analyze` → vision LLM).
- Env: `LLM_BASE_URL` (Ollama `http://host.docker.internal:11434` dev / vLLM prod), `LLM_MODEL` (`gemma4:e4b` por defecto), `MAX_IMAGE_DIM`, `CACHE_TTL_SECONDS`.
- Este LLM es **externo a `models/` de textFlow** (fase A decisión: *externo al bundle*).
- Air-gapped: provisionar por separado:
  - **Ollama** (recomendado doc §18): en máquina online `ollama pull <model>`, exportar `ollama export` / copiar `~/.ollama/models` al bundle (configurar `OLLAMA_MODELS`), en target `ollama import` / copia. Documentar instalación Ollama, `pull`, `export/transfer`, `import`.
  - **vLLM** (prod): empaquetar imagen vLLM + weights del modelo vision; desplegar con `vllm serve`.

### 7. Inference LLM (hechos)

- Servicio: `cmd/inference-worker` consume runtime OpenAI-compatible (`LLM_URL=http://vllm:8000/v1`, `LLM_MODEL`, `LLM_TIMEOUT`, `INFERENCE_MAX_CONCURRENCY`, `INFERENCE_WORKER_REPLICAS`).
- Air-gapped igual que (6): externo, no en `models/`. Documentar `vLLM` vs `Ollama` por separado (§19) y cómo provisionar cada uno offline. Fase A: *externo al bundle*.

## Procedimientos canónicos

### Online prep machine

```bash
# 0. Requisitos host: Docker, jq, bash, curl, espacio (~15 GB con large-v2 + docling)
make setup-models
# → deploy/docker/download_models_offline.py
#   HF: gliner, deberta, bge-m3, whisper-large-v2
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
- Faster-Whisper offline: `WhisperModel(path, local_files_only=True)` + `MODEL_PATH=/models`
