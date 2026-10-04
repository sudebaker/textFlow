# textFlow — Operations

## Day-2: health, logs, metrics

```bash
docker compose -f deploy/docker/docker-compose.yml ps
curl -f http://localhost:8080/health
curl -f http://localhost:5001/openapi.json    # docling
curl -s http://localhost:8080/metrics | head   # orchestrator (ia_text_*)
curl -s http://localhost:8001/metrics | head   # extraction-worker
open http://localhost:3000  # Grafana (deploy/docker/grafana, dashboard textflow-overview, §T3.2)
open http://localhost:9090  # Prometheus (deploy/prometheus, alerts deploy/prometheus/alerts.yml)
```

Métricas con P50/P95: Grafana `textflow-overview` (`ia_text_job_duration_seconds_bucket`, `ia_text_job_step_duration_seconds_bucket`, `*_queue_time_seconds_bucket`, `ia_text_queue_depth`). Alertas §Deploy prometheus `alerts.yml`.

## Upgrades / Rollback

Los jobs en vuelo usan el DAG de `configs/pipeline.json` fijado en la imagen. **D3/D4**: el DAG es declarativo (`PipelineDefinition` en `pkg/worker_common/pipeline_config.py`), routing fan-out en `extraction-worker/worker.py` y `required_steps` en `completion-worker`.

### Migración big-bang con drain (recomendada)

1. Stop admission: `lb pause / no POST /v1/documents`.
2. Drain: esperar `redis-cli ZCARD active_jobs == 0` (`JobTimeout=60m` acota el peor caso; `ExpireStuckJobs` solo expira `pending/processing/extracting`).
3. Deploy: nuevas imágenes (orchestrator con `pipeline_version`, workers con `configs/pipeline.json` actualizado).
4. Resume admission y smoke `GET /v1/documents/:id` con job `+ features=["inferences"]`.

### Rollback

Cualquier `dist/` previo es self-contained (`images/*.tar.gz`, `models.tar.gz`, `config/`, `install.sh`, `MANIFEST.txt`). Reinstalar:

```bash
bash install.sh --bundle dist-<previous>/
docker compose up -d --force-recreate
```

`install.sh` hace `docker load` + `tar -xzf models.tar.gz` + `cp config/.env.example` si es preciso y valida (`deploy/package/verify-installation.sh`).

## Bundle & transferencia

```
make package                      # produce dist/ (offline, 43+ GB con large-v2+docling)
bash deploy/package/verify-bundle.sh [--dist dist]
make deploy HOST=10.0.0.5          # rsync dist/ al target
ssh $HOST "bash ~/…/dist/install.sh"
bash deploy/package/verify-installation.sh  # en destino
```

`dist/MANIFEST.txt` es la fuente de verdad: commit, timestamp, Docker version, `*.tar.gz` digests, `models.tar.gz` sha256/tamaño.

> No duplicar manualmente `rabbitmq:3.12` vs `3.13`: `deploy/package/package.sh` deriva `EXTERNAL_IMAGES` de `docker compose config --format json` (`build == null`).

## Retención y GC

- Job keys: TTL 24h (`Orchestrator:job:{id}:*`, `active_jobs` ZSET). Control + steps + `micro_inferences_raw` en Redis refs.
- Blobs: FS `data/{ab}/{cd}/{sha256}.bin` (65k buckets, `FSStore` en `pkg/worker_common/artifact_store.py`). **No TTL** en FS.
- Lifecycle GC (Fase 2 T2.2): `python -m pkg.worker_common.artifact_gc --dry-run --min-age 24h` (reachability `orchestrator:job:*:text|chunks|embeddings|inference_embeddings|results` → scan FS → borrar huérfanos viejos). Liberación `artifact_gc_bytes_reclaimed`.

## Vision OCR (extracción visual)

**Estado:** flag `VISION_OCR_ENABLED=false` (default); solo el gateway funciona en solo-logging vía `extraction-worker:8004`.

1. **Medición (C.5): rate(extraction_worker_quality_gate_documents_total[1h])** por `decision кадров pass|suspect` → %docs que abandonan el fast path; `extraction_worker_vision_pages_total{backend}` para `vision|vision_error|degraded`.
2. **Calibración (F.3) — SOLO con serving real:** la herramienta (Herramienta y cadena son backend-agnóstico, `VISION_LLM_BASE_URL`/`VISION_MODEL`: vLLM en prod — p. ej. H200; Ollama mac-mini es DEV compartido y sus métricas NO definen parámetros; la regla de swop-evict vision se resuelve con weight-cache GPU).
3. **Actendente (flip de producción, decisión explícita del owner):** activar con `VISION_OCR_ENABLED=true` en `deploy/docker/.env` + `docker compose up -d extraction-worker` → le valida de test con job de prueba (spreadsheet=full, features=["inferences"]) por `tools/client` y `GET /v1/documents/:id` → estados de accés.
4. **Cache:** claves `vision:*` solo-SERVICE cache con TTL 604800 — **sin GC de FS**; sin TTL ni blob en FS para esta ruta (provenance job-scoped en Redis; el conteo `cache_hit` Prometheus SI correcto, mientras el payload del hit conserva `cached:false` de la primera escritura — nit gemela H.1).
5. **Budgets (spec §18): VISION_OCR_TIMEOUT** por página; **`VISION_OCR_MAX_PAGES_PER_DOCUMENT`/`MAX_SECONDS_PER_DOCUMENT`** por doc; cancelación cooperativa ya en el loop (`raise_if_cancelled` entre páginas) y estado `degraded` + `fallback_whole_document` para rescate del blob.

## Troubleshooting correlacionado

Ver `docs/TROUBLESHOOTING.md` (Docker/GPU/modelos/Docling/colas/LLM) y `docs/MODELS.md` para adquisición y verificación de cada modelo (`local_files_only=True`, `HF_HUB_OFFLINE=1`).
