# textFlow — Performance

> Basado en `docs/GPU.md`, `pkg/metrics/metrics.go` (`ia_text_*`) y
> `deploy/prometheus/alerts.yml`. Hasta tener benchmarks en el hardware
> objetivo, ningún valor aquí es el "óptimo".

## Qué medir (separar time_to_text vs time_to_processed_document)

- **time_to_text**: ingest → `extraction` (`stage.completed(extraction)`, `text_ref`).
- **time_to_processed_document**: ingest → `completion` final (`GET /v1/documents/:id` → `completed`, `results-data/{jobID}.json` con embeddings/entities/inferences según `features`).
- Por etapa: `queue time` (`queued_at` → consume) vs `stage duration` (`stage.started → stage.completed`).
- P50/P95, `throughput` (jobs/s, pages/sec, chunks/sec, tokens/sec), `GPU utilization`, `VRAM`, `CPU`, `queue depth`.

## Dónde medir

- Grafana `textflow-overview` (Fase 3 T3.2): panels `Queue time P50/P95`, `Job duration P50/P95`, `Stage duration P50/P95` (`histogram_quantile(0.95, sum by(le) rate(ia_text_job_duration_seconds_bucket[5m]))` etc.), `Throughput`, `Errors`, `Queue depth` (`ia_text_queue_depth`, `consumer_lag`).
- Prometheus scrape: orchestrator `8080/metrics`, workers `8001-8006/metrics`, `redis-exporter:9121`, `rabbitmq:15692/metrics`. Alertas `deploy/prometheus/alerts.yml` (queue saturada, jobs stuck, high failure rate).
- Perfiles: `?profile=fast|balanced|full`; `fast = extraction+metadata`, `balanced==full = extraction+embeddings+entities+metadata` (full intencionalmente == balanced, §8; inferences solo vía `-f/features=[inferences]`, Fase 4).

## Cómo reproducir benchmarks

### Docling (cuello de botella PDF — `docling-serve`, no orchestrator)

Fijar **dos corpus**: uno CPU y el mismo para CUDA. Sweep separado:

- `DOCLING_PERF_PAGE_BATCH_SIZE: 4/8/16/32/64`
- `EXTRACTION_CONCURRENCY: 1/2/4/5`

Medir `pages/sec`, `queue time`, `conversion time`, `P50/P95`, `GPU util`, `VRAM`. Óptimo = `throughput ↑`, `P95` estable, sin OOM, misma calidad. Referencias oficiales Docling: [usage/gpu](https://docling-project.github.io/docling/usage/gpu/), [api_server/deployment](https://docling-project.github.io/docling/usage/api_server/deployment/).

> No fijar un `page batch` arbitrario como óptimo; debe salir del benchmark de este hardware.

### Embeddings / Entities

- `EMBEDDING_BATCH_SIZE_GPU: 32/64/96/128` → `chunks/sec`, `tokens/sec`, P50/P95, VRAM.
- `GLINER_BATCH_SIZE` — no confundir con `EXTRACTION_CONCURRENCY`.

## Evitar regresiones

Ninguna optimización (streaming hash T4.1, adaptive inference T3, `TORCH_COMPILE=true`) se considera terminada hasta reproducir P50/P95 estables + `make test` verde en el hardware de producción.
