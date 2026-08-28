# textFlow — Troubleshooting

## Docker / Compose

- **Imagen no encontrada en air-gapped.** Verifica `dist/images/*.tar.gz` + `MANIFEST.txt` (digests). Carga: `for f in images/*.tar.gz; do docker load < "$f"; done`. Validación previa: `bash verify-bundle.sh`.
- **Permisos `permission denied` en volúmenes.** `artifacts-data` y `uploads-data` requieren `chmod`. Recrea: `docker compose down -v && up -d`.
- **`docker compose config` falla.** `MODELS_PATH` no set cuando cargas `docker-compose.yml` fuera de `deploy/docker` → usa `MODELS_PATH=/tmp docker compose config`.

## GPU

- **`nvidia-smi` no visible en host.** Instala driver NVIDIA + `nvidia-container-toolkit`. Test: `nvidia-smi` y `docker run --rm --gpus all nvidia/cuda:12.8.0-base nvidia-smi`.
- **GPU no visible dentro del contenedor.** `docker info | grep -i runtime` debe listar `nvidia`. Si usas `runtime: nvidia` vacío en nuevo Docker, cambia a `deploy: resources: reservations: devices:` (CDI).
- **`nvidia-uvm` major 511 vs 510 (`CUDA unknown error`).** Usa el workaround documentado en `docs/GPU.md` (bind-mount devices + `nvidia-smi` bin) — no uses `--gpus` en ese host.
- **`OSError: CUDA not available` en Docling/health.** `DOCLING_DEVICE` quedó en `cpu`. Con override: `docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d docling`.
- **OOM (Docling/workers).** Baja `DOCLING_PERF_PAGE_BATCH_SIZE` o `EXTRACTION_CONCURRENCY`, o `EMBEDDING_BATCH_SIZE_GPU`. Monitor `VRAM` en Grafana (`textflow-overview`).

## Modelos

- **`HF_HUB_OFFLINE=1` pero intenta acceder a Hugging Face.** Algún env quedó en `false` o `local_files_only=False` en el loader. Busca el modelo faltante en `docker compose logs entities-worker | grep -i "http\|hub"`.
- **`Tokenizer incorrecto / modelo corrupto`.** Valida `MODEL_REQUIRED_FILE_GROUPS` en `download_models_offline.py:_is_snapshot_complete`. Re-descarga: `rm -rf models/huggingface_cache && make setup-models`.
- **`No such file: /models/whisper/large-v2/model.bin`.** `WHISPER_MODEL` no coincide con el repo descargado (`Systran/faster-whisper-large-v2`). Alinea `WHISPER_MODEL` ↔ `models/whisper/<size>` o re-descarga.
- **Ruta incorrecta.** `MODELS_PATH` resuelto relativo a `deploy/docker` (`../../models` dev) ≠ `../models` en target (`install.sh` lo corrige). Verifica el mount: `docker inspect textflow-docling | grep -A2 Mounts`.

## Docling

- **Fallback a CPU aunque GPU esté disponible.** Imagen sin CUDA (`docling-serve:latest` vs `cu128-0.12.0`). Usa el override GPU y confirma `docker inspect docling | grep -i cuda`.
- **Artifacts ausentes (`FileNotFoundError` en docling).** `models/docling/` no existe y el mount apunta a vacío. Ejecuta `download_models_offline.py:download_docling_models` o usa solo la imagen CUDA bundled.
- **OCR no produce texto en escaneado.** `DOCLING_DO_OCR=false` por defecto — escanéalo con `ocr` explícito o activa `DOCLING_DO_OCR=true` para ese profile. `DOCLING_OCR_ENGINE=rapidocr` con backend Torch solo si hay GPU (§14).
- **`exiftool` timeout 10s / no instalado.** `extract_metadata_deep` ya degrada silenciosamente (fast metadata preservado). Verifica `exiftool -ver` en `extraction-worker`.

## RabbitMQ / Redis

- **Colas acumuladas (`queue_depth` crece, 0 consumers).** Args DLX inconsistentes entre Go y Python: `x-dead-letter-exchange` debe coincidir en `internal/broker/rabbitmq.go:declareQueue`, `pkg/worker_common/{base,async_base,rabbitmq_*}.py`. Síntoma: `PRECONDITION_FAILED` en logs.
- **Backlog no drena.** `AdmissionController` rechaza (`503 queue_depth`) cuando `ia_text_queue_depth > QueueDepthRejectThreshold`. Sube threshold o escala workers.
- **Retries infinitos / DLQ lleno.** `MAX_RETRIES=3`, `DELAYED_EXCHANGE_ENABLED=true` requiere plugin `rabbitmq_delayed_message_exchange`. Sin plugin → fallback `nack(requeue=True)` + `sleep`.
- **Job stuck `pending/processing` sin avanzar.** `CompletionWorker.ExpireStuckJobs` solo expira `pending/processing/extracting` a nivel job; `ZCard active_jobs` nunca baja → wait drain antes de migrar DAG. Revisa `orchestrator:job:{id}:steps` + `orchestrator:job:{id}:error`.

## LLM (Ollama / vLLM)

- **`LLM_URL` no disponible / `502`.** Verifica `curl http://<LLM_URL>/v1/models` y `LLM_MODEL` coincide con `vllm serve --model`. `ollama list` en host.
- **`LLM call timed out` / saturación.** Sube `INFERENCE_LLM_TIMEOUT` / `INFERENCE_TIMEOUT_DECAY_FACTOR`, baja `INFERENCE_MAX_CONCURRENCY`, o escala `INFERENCE_WORKER_REPLICAS`. Métrica `inference_worker_in_flight`.
- **Inference facts vacíos / calidad baja.** `LLM_MODEL` no es instruct (`qwen3-coder` vs `bge`): inference requiere VLM/instruct. Prueba prompt directo con `curl -X POST $LLM_URL/v1/chat/completions`.
- **Modelo vision no devuelve OCR.** El endpoint `POST /analyze` (`deploy/docker/image-analyzer`) hace resize `MAX_IMAGE_DIM=1024` y cache `image:{sha}` + prompt+model (§1 p1 fix). Purga `redis-cli DEL "image:<digest>"`.
