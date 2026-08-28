# textFlow — Compatibility Matrix

Leer junto a `docs/MODELS.md` (qué se descarga) y `docs/GPU.md` (cómo se valida la GPU).

| Componente | Versión fijada | CUDA / Runtime | Python | Driver mínimo / OS | Notas |
|---|---|---|---|---|---|
| Docling-serve (CPU) | `quay.io/docling-project/docling-serve:latest` | — | — | — | artifacts externos `models/docling/` si CPU (Fase A dual) |
| Docling-serve (GPU) | `quay.io/docling-project/docling-serve:cu128-0.12.0` | CUDA 12.8 | — | Host CUDA 12.8 compatible | Override `deploy/docker/docker-compose.gpu.yml` |
| Faster-Whisper | `Systran/faster-whisper-large-v2` (+ `tiny…large-v3` intercambiables) | CPU `int8` / CUDA `float16` | `python:3.10-slim` (whisper) | `ffmpeg` | `MODEL_SIZE=large-v2`, `MODEL_PATH=/models` |
| GLiNER / DeBERTa | `urchade/gliner_small-v2.1` + `microsoft/deberta-v3-small` | CPU / CUDA (entities-worker) | 3.11 | `torch` compatible con CUDA | Validados con `Glb ner.from_pretrained` `local_files_only=True` |
| BGE-M3 | `BAAI/bge-m3` | CPU / CUDA | 3.11 | — | `SentenceTransformer` `local_files_only=True` |
| Image / Inference LLM | `LLM_MODEL` en `LLM_BASE_URL` (Ollama/vLLM) | externo al bundle | — | — | OpenAI-compat `/v1/chat/completions` (image resize `MAX_IMAGE_DIM=1024`) |
| RabbitMQ | `rabbitmq:3.13-management` | — | — | — | deriva de `deploy/docker/docker-compose.yml` (no `3.12`) |
| Redis | `redis:7-alpine` | — | — | — | `maxmemory 1GB + noeviction` |
| Prometheus / Grafana / exporter | `prom/prometheus:v2.53.0`, `grafana/grafana:11.1.0`, `oliver006/redis_exporter:v1.67.0` | — | — | — | scrape `orchestrator:8080/metrics`, workers `8001-8006` |

Actualizar esta matriz cuando se pinne un nuevo digest (`dist/MANIFEST.txt` es la fuente de verdad del bundle concreto).
