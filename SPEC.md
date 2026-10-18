# Spec: Per-File Processing Profiles (`perfiles-por-fichero`)

**Status**: Draft — awaiting human review
**Change type**: Breaking (profile rename, local→remote embeddings, new env vars)
**Migration**: drain + big-bang (no dual-run), per DAG migration runbook

## Objective

Today a processing profile (`fast|balanced|full`) can only be chosen **per job**; batch
documents inherit one global profile; spreadsheets silently override the requested
profile; micro-inference dispatch is hard-wired to the entities step; embeddings require a
local GPU model; and image/audio results never reach the final JSON.

End users must be able to choose a processing profile **per file** (never model IDs). The
file type selects the entry stage; the profile composes the applicable downstream stages.
The team configures model endpoints by capability (embeddings, inference LLM, OCR, image
description) via environment variables.

**Success looks like**: a single batch mixes `rag` and `full` documents, each producing its
own stage set; a `rag` job produces chunks + embeddings + inferences with **no** entities
step; image `full` returns OCR text plus a semantic description while `rag` returns
description `null`; no local model weights are loaded anywhere.

### Profile semantics

| Profile | Stages |
|---|---|
| `rag` | entry (extraction/audio/image) + chunking + embeddings + inferences. **No entities, no metadata.** |
| `full` (default) | `rag` + entities + metadata; for images also semantic `description`. |

`fast` and `balanced` are **removed** (breaking rename). Default is `full`.

## Requirements

### processing-profiles

- **Profile set `rag|full`**: the system MUST accept exactly two profiles; default MUST be `full`.
  - GIVEN a job with no profile → `full` applies and all applicable stages run.
  - GIVEN a document job with `rag` → extraction, embeddings and inferences run; no entities/metadata step is required.
- **Type×profile composition**: the entry stage is selected by file type (document/spreadsheet/audio/image) and downstream stages are composed by profile — **compose, never override**. The spreadsheet short-circuit is removed; spreadsheets, audio and images honor compatible downstream stages.
  - GIVEN a spreadsheet with `rag` → entry `extraction`, downstream `embeddings`+`inferences`, no entities.
  - GIVEN an image with `full` → entry `image`, `description` capability applies.
  - GIVEN an image with `rag` → entry `image`, description not requested.
- **Per-file profile selection**: profile MUST be accepted per file on single ingestion and per document in batch. Batch documents MAY omit it and inherit the batch profile (default `full`).
  - GIVEN a batch whose documents carry `rag` and `full` → each document runs its own profile's stages.
  - GIVEN a batch document without profile → it inherits the batch profile.
- **Inference dispatch decoupled from entities**: micro-inferences MUST be dispatched from the entry-stage fan-out (extraction/audio/image), independent of the entities step, preserving the `inferences:remaining/:total` counter-before-publish race fix.
  - GIVEN a `rag` document job (no entities step) → inference chunks are published and inferences appear in the result.
  - Accepted consequence: inference messages carry `entities: []` always (the entry stage runs before entities), so the inference prompt no longer receives entity context. Recovering it would require a second, entities-triggered pass and is out of scope.

### pipeline-dag-declaration (MODIFIED)

- The DAG MUST declare profiles `rag|full` and per-type applicable stages, and the spreadsheet pipeline MUST NOT override the requested profile.
  - GIVEN pipeline.json `rag` → steps for a document exclude entities and metadata.
  - GIVEN a spreadsheet with `full` → it honors profile downstream instead of forcing extraction+entities.

### batch-ingestion (MODIFIED)

- The batch MUST accept an optional per-document `profile`; when absent it inherits the batch profile (default `full`).
  - GIVEN a batch where one document sets `rag` → that document runs `rag` while others run `full`.
  - GIVEN a batch document with an invalid profile → that document is rejected with a clear error in `rejected[]` and siblings are unaffected; all documents invalid → HTTP 400.

### image-ocr-description (ADDED)

- OCR and semantic description MUST be distinct, env-configured endpoints: `OCR_BASE_URL`/`OCR_MODEL` (OCR) and `IMAGE_BASE_URL`/`IMAGE_MODEL` (description). `/analyze` stays OCR; a new `/describe` provides description.
  - GIVEN separate endpoints → an image `full` job calls each on its own endpoint.
- Description is requested only under `full`; `rag` is OCR-only.
  - GIVEN image `full` → `image_description` is non-null.
  - GIVEN image `rag` → `image_description` is null and OCR text is present.
- Description is a **single field** (`:metadata:image.description` → `image_description`), NEVER chunked or embedded. OCR text is the RAG field.

### remote-embeddings (ADDED)

- Embedding MUST go through `POST /v1/embeddings` (`EMBEDDINGS_BASE_URL`/`EMBEDDINGS_MODEL`), 1024-dim bge-m3 parity, with batching, timeouts, retries and error mapping. It MUST replace all three local loading paths (embeddings-worker, inference-embeddings helper, completion-worker) with **no local fallback**.
  - GIVEN a reachable endpoint → 1024-dim vectors are returned and cached by content hash `sha256(model+text)` (`emb:{hash}`, TTL `EMBEDDINGS_CACHE_TTL_SECONDS`, default 7d).
  - GIVEN an unreachable endpoint → the error is mapped and the job fails with a clear message (no fallback).
  - GIVEN partial batch failure → successful vectors are returned, failures logged, and `embeddings_degraded: true` is surfaced.
  - GIVEN identical text twice → exactly one HTTP call (cache hit).

### job-result-format (MODIFIED)

- The final JSON MUST emit explicit `image_ocr_text` and `image_description` (null when not requested), plus `metadata:image`, `metadata:audio` and `audio_segments` when present. Text remains the common RAG field.
  - GIVEN image `full` → `image_ocr_text` + non-null `image_description`.
  - GIVEN image `rag` → `image_ocr_text` present, `image_description` null.
  - GIVEN audio → language, duration, diarization and segments present.
  - GIVEN a document → common RAG fields (text, chunks, entities, inferences) present.
- Media Redis keys (`:metadata:image`, `:metadata:audio`, `:audio_segments`) MUST use `setex(JOB_TTL_SECONDS=86400)` via a shared helper. No `:image_description` key.
- `SchemaVersion` is removed from `JobResults` (the migration escape hatch is `JobMessage.pipeline_version`, not a result schema version).

### Cross-cutting

- **Inference LLM env separation**: the inference worker MUST read `INFERENCE_BASE_URL`/`INFERENCE_MODEL` ONLY; `LLM_URL`/`LLM_MODEL` are removed with no silent dual source. Unset → startup error / inferences disabled.
- **Docs and test client**: `docs/API.md`, `docs/CONFIGURATION.md`, `docs/MODELS.md` and `tools/client` MUST document `rag|full`, the breaking rename, and the new env vars; the client MUST send `profile`.
- **Migration runbook**: drain/big-bang (no dual-run), Go↔Python queue-arg parity for any new queues, Redis TTLs for new keys.

## Tech Stack

- Go 1.x orchestrator (Gin, port 8080) + Go resource-manager (9090)
- Python workers (pika/Redis): extraction, embeddings, entities, metadata, completion, inference, audio, image
- RabbitMQ (DLX + `x-max-length`/`x-overflow`), Redis (`maxmemory 1gb`, `noeviction`)
- Remote model endpoints, OpenAI-compatible: `/v1/embeddings` (bge-m3, 1024-dim), inference LLM, image OCR + description
- Air-gapped deployment: no internet at build or runtime

## Commands

```bash
make test                                       # all Go tests
make test-python                                # all Python tests (per-dir pytest loop)
go test ./cmd/orchestrator/... -run TestBatch    # focused Go (W1)
pytest cmd/image-worker/tests/ -v                # focused Python (W3)
pytest pkg/worker_common/tests/test_pipeline_config.py -v
make lint && make format                         # golangci-lint; go fmt + black + isort
make build                                       # all binaries → bin/
make infra-up                                    # RabbitMQ, Redis, Docling
```

## Project Structure

```
cmd/          orchestrator (Go), resource-manager (Go), Python workers
internal/     Go: broker/, config/, events/, middleware/, models/, redis/
pkg/          shared: logging/, metrics/, worker_common/, image_client/, events_python.py
configs/      pipeline.json (declarative DAG)
deploy/docker docker-compose, image-analyzer, vision-ocr, whisper
docs/         API.md, CONFIGURATION.md, MODELS.md, OPERATIONS.md, ARCHITECTURE.md
tools/client  Go test client
tasks/        plan.md + todo.md (this spec's implementation plan)
```

## Code Style

Follow `AGENTS.md`. Example (Python):

```python
# Standard library
import logging
from typing import Dict, List, Optional

# Third-party
import redis

# Local
from pkg.worker_common.pipeline_config import PipelineDefinition
```

- Python: classes PascalCase, functions/vars snake_case, constants UPPER_SNAKE, private `_underscore`. 3 import sections (stdlib/third-party/local), alphabetical.
- Go: exported PascalCase, unexported camelCase; wrap errors with `%w`; log via zerolog with structured fields (`logger.Error().Err(err).Str("job_id", jobID)`).
- No comments unless they explain non-obvious intent. Config via `pydantic_settings.BaseSettings` (Python) / struct env tags (Go).

## Testing Strategy

Strict TDD: **every task writes a failing test first (RED), then the minimum implementation (GREEN)**, then runs the full suite for regressions.

| Level | Scope | Tooling |
|---|---|---|
| Unit | resolver type×profile×chunking (4×2), `normalizeProfile`, batch validation/rollback | pytest table-driven; Go `testing` + `testify` |
| Unit | embeddings cache hit/miss, partial degrade, total failure | mock HTTP + fake Redis |
| Unit | media-key TTL (`setex` 86400) | fake Redis, assert TTL |
| Integration | dispatch race (counters before publish), OCR vs description, description never embedded | Redis container + mock LLM |
| Contract | Go↔Python queue-arg parity | `pytest pkg/worker_common/tests/test_queue_declaration_sites.py -v` |
| E2E | mixed-profile batch | `tools/client` |

## Boundaries

- **Always**: write the failing test first; run the focused test + `make test` / `make test-python` before each commit; keep Go↔Python RabbitMQ queue-arg parity in the SAME commit; build Go binaries to `bin/`; use `setex` TTLs on job-scoped Redis keys; cite the spec section in the commit message.
- **Ask first**: changing `configs/pipeline.json` schema beyond this spec; adding dependencies; modifying Docker build files; changing the queue argument table; touching anything that affects the drain runbook.
- **Never**: commit secrets, binaries (`bin/*`, `tools/client/client`), or `models/`; add network access to builds (air-gapped: no `wget`, no HF Hub calls); load local model weights as a fallback for embeddings; remove or skip failing tests without approval; leave `LLM_URL`/`LLM_MODEL` as a silent dual source.

## Success Criteria

- [ ] Batch job with mixed per-file profiles (`rag` + `full`) produces per-file stage sets per the type×profile matrix.
- [ ] A `rag` job runs extraction + chunks + embeddings + inferences with **no** entities step.
- [ ] Spreadsheet `rag` yields non-empty chunks + embeddings and no entities step (short-circuit removed).
- [ ] Batch document with invalid profile → `rejected[]` entry, siblings unaffected; all-invalid → HTTP 400.
- [ ] Image `full` returns `image_ocr_text` + non-null `image_description`; image `rag` returns `image_description: null`; description is never chunked/embedded.
- [ ] No local model-loading path remains; embeddings served from `EMBEDDINGS_BASE_URL`; identical text twice → one HTTP call.
- [ ] Inference worker reads `INFERENCE_BASE_URL`/`INFERENCE_MODEL` only; unset → startup error.
- [ ] Media Redis keys carry TTL 86400.
- [ ] Docs (`API.md`, `CONFIGURATION.md`, `MODELS.md`) and `tools/client` updated; Go↔Python parity tests green.

## Open Questions

None blocking. Settled during design (recorded here so they are not re-litigated):

- Spreadsheet chunking reuses the entry-stage chunker; chunk-quality tuning deferred (owner: data-platform).
- Audio diarization stays out of scope.
- Image description is a single non-embedded field.

Operational prerequisite before implementation of W4: a reachable dev embeddings endpoint
(`EMBEDDINGS_BASE_URL`, mac-mini via `docker_default`).
