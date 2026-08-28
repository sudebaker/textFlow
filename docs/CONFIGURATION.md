# textFlow — Configuration Reference

> Fuente: `.env.example` + `deploy/docker/docker-compose.yml` / `docker-compose.gpu.yml`.
> Dead variables explícitamente marcadas; si una env no aparece aquí es que no se lee.
> Unverified: revisar con `rg -n "os.getenv|env:"` antes de publicar nuevas env.

## Núcleo

| Variable | Default | Tipo | Valores | Servicio | Efecto | Recomendación |
|---|---|---|---|---|---|---|
| `HF_HUB_OFFLINE` | `1` | bool | `1` air-gapped | todos | `huggingface_hub` no toca internet | **no cambiar** en prod |
| `TRANSFORMERS_OFFLINE` | `1` | bool | `1` | todos | transformers sólo local | **no cambiar** |
| `MODELS_PATH` | `../../models` | path | host path | compose | bind-mount `huggingface_cache`, `whisper`, `docling/models` | prod: `../models` (ver `install.sh`) |
| `RABBITMQ_URL` | `amqp://…@rabbitmq:5672/` | url | `amqp://` | todos | broker | nunca `guest:guest` en prod |
| `REDIS_URL` | `redis://redis:6379` | url | `redis://` | todos | estado + pub/sub + artifact refs | sin ACL abierta |
| `HTTP_PORT` | `8080` | int | 1024+ | orchestrator | API + `/metrics` | detrás de reverse-proxy |
| `LOG_LEVEL` | `info` | enum | `debug/info/warn/error` | todos | verbosidad | prod `info` |

## Docling / Extraction

| Variable | Default | Servicio | Notas |
|---|---|---|---|
| `DOCLING_DEVICE` | `auto` → CPU | extraction-worker, docling | CPU dev; GPU override `cuda` (`docker-compose.gpu.yml`) |
| `DOCLING_NUM_THREADS` | `4` | docling | hilos Docling |
| `EXTRACTION_CONCURRENCY` | `5` | extraction-worker | jobs Docling en paralelo (async polling, no bloquea) |
| `PREFETCH_COUNT` | `5` | RabbitMQ qos | fallback alias de `EXTRACTION_CONCURRENCY` |
| `EXIFTOOL_PATH` / `EXIFTOOL_TIMEOUT` | `/usr/bin/exiftool`, `10s` | extraction-worker | exiftool deep via `asyncio.to_thread`; timeout anti-cuelgue |
| `DOCLING_DO_OCR` / `DOCLING_OCR_ENGINE` | `false` / `rapidocr` | extraction-worker | §14: no activar globalmente; solo escaneados |
| `MAX_SPREADSHEET_ROWS` | `2000` | orchestrator | spreadsheet guard |

## Embeddings / Entities / Metadata

| Variable | Default | Servicio | Notas |
|---|---|---|---|
| `EMBEDDINGS_DEVICE` | `cpu` | embeddings-worker | `cuda` si GPU |
| `EMBEDDING_BATCH_SIZE_GPU` | `64` | embeddings-worker | bench 32/64/96/128 (GPU); `*_CPU=2` |
| `ENTITIES_DEVICE` | vacío (auto) | entities-worker | `cuda` explícito |
| `GLINER_BATCH_SIZE` | `16` | entities-worker | `chunks/model call` — no confundir con `EXTRACTION_CONCURRENCY` |
| `ENTITY_TYPES` / `ENTITY_THRESHOLD_*` | `PERSON,ORGANIZATION,…` | entities-worker | umbrales por label |
| `DEDUPLICATION_ENABLED` | `false` | entities-worker | `FUZZY_MATCH_THRESHOLD=0.85` solo si `true` |
| `REGEX_ENTITY_EXTRACTOR_URL` | `http://regex-entity-extractor:8081` | entities-worker | paralelizado con GLiNER (Fase 2) |

## Inference (facts per chunk)

| Variable | Default | Servicio | Notas |
|---|---|---|---|
| `LLM_URL` | vacío | inference-worker | OpenAI-compat (vLLM `http://vllm:8000`, Ollama) — externo al bundle |
| `LLM_MODEL` | `qwen3-coder` | inference-worker | debe coincidir con el modelo cargado en el LLM |
| `INFERENCES_QUEUE` | `inferences` | inference-worker | activada por `-f` / `feature_extras` |
| `INFERENCE_BATCH_ENABLED/SIZE/TIMEOUT_MS` | `true/3/500` | inference-worker | cache `INFERENCE_CACHE_*`, `MAX_CHUNK_WORDS=5000` |
| `INFERENCE_ADAPTIVE_ENABLED` + `MAX/MIN_CONCURRENCY, TIMEOUT_DECAY, COOLDOWN` | `false/16/1/2/30` | inference-worker | OFF hasta benchmarks GPU; `INFERENCE_LLM_TIMEOUT=60`, `RETRIES=2` |

## Audio / Image

| Variable | Default | Servicio | Notas |
|---|---|---|---|
| `WHISPER_MODEL` | `large-v2` | whisper | `tiny…large-v3` → `Systran/faster-whisper-*` ; `MODEL_PATH=/models` |
| `WHISPER_DEVICE / COMPUTE_TYPE` | `cpu / int8` | whisper | GPU: `cuda / float16` |
| `WHISPER_URLS`, `TIMEOUT`, `MAX_RETRIES` | `http://whisper:8080`, `300`, `3` | audio-worker | |
| `AUDIO_CHUNK_MAX_CHARS` / `MAX_AUDIO_SIZE_MB` | `1500 / 500` | audio-worker | |
| `MULTIMODAL_LLM_URLS / TIMEOUT / RETRIES` | `http://multimodal-llm:8000 / 120 / 3` | image-worker + image-analyzer | Ollama dev `host.docker.internal:11434`, vLLM prod; `MAX_IMAGE_DIM=1024`, `CACHE_TTL_SECONDS=86400` |
| `IMAGE_QUEUE / AUDIO_QUEUE` | `image / audio` | image/audio-worker | reemplazadas por `image_replaces_extraction` / `audio_replaces_extraction` |

## RabbitMQ / Recursos

| Variable | Default | Notas |
|---|---|---|
| `EXTRACT_QUEUE / EMBEDDINGS_QUEUE / ENTITIES_QUEUE / METADATA_QUEUE / COMPLETION_QUEUE` | `extract_text …` | dummy `PIPELINE_CONFIG_PATH=/app/configs/pipeline.json` puede re-route |
| `PREFETCH_COUNT` | `5` | qos por worker |
| `MAX_RETRIES` / `DELAYED_EXCHANGE_ENABLED` | `3 / true` | `document_processor_delayed` plugin vs `nack(requeue)` fallback |
| `MESSAGE_TIMEOUT` | `30s` | handler timeout (orchestrator) |
| `RESOURCE_MANAGER_PORT` / `GPU_MONITORING_ENABLED` | `9090 / false` | nvidia-smi opcional |

**Verificación antes de publicar:** `make docs` o script CI que cruce `grep -n os.getenv .env.example` → envs en tablas. Variables marcadas *dead* en Compose/`make` pero no leídas desde código deben eliminarse o documentarse como `deprecated`.
