# Tasks: Per-File Processing Profiles (`perfiles-por-fichero`)

**Plan**: `tasks/plan.md` · **Spec**: `SPEC.md`
**Cadence**: one task per session — RED → GREEN → focused test → full suite → commit → stop
**Delivery**: W1 → W2 → W3 → W4 → W5 as chained PRs (stacked-to-main)

Legend: `[token]` names the Engram artifact this task came from (`sdd/perfiles-por-fichero/*`).
Task IDs preserve the original design-task numbering for traceability.

---

## W1 — Profile model + batch validation (PR 1)

**Unit acceptance (`1.8`)**: spreadsheet `rag` yields non-empty chunks + embeddings and no
entities step; batch document with invalid profile → `rejected[]`; all-invalid → HTTP 400.
**Rollback boundary**: `configs/pipeline.json`, `pipeline_config.py`, `batch_models.go`, `batch.go`, `job.go`.

### Task 1.1 — RED: `normalizeProfile` contract
- **Description:** Failing Go test pinning the `rag|full` contract: invalid profile → error/fallback documented, empty → `full`.
- **Acceptance criteria:**
  - [x] `TestNormalizeProfile` fails before implementation, passes after.
  - [x] Empty input resolves to `full`; `rag`/`full` pass through; other values are rejected per D1.
- **Verification:**
  - [x] Tests pass: `go test ./cmd/orchestrator/... -run TestNormalizeProfile`
- **Dependencies:** None
- **Files likely touched:** `cmd/orchestrator/main_test.go`
- **Spec**: processing-profiles (Profile set)

### Task 1.2 — GREEN: `normalizeProfile()` implementation
- **Description:** Implement `normalizeProfile()` in the orchestrator: accept exactly `rag|full`, default `full`, reject others (breaking rename: `fast`/`balanced` removed). Returns `(string, error)`; both handlers validate before creating job state and answer HTTP 400 `invalid_profile`.
- **Acceptance criteria:**
  - [x] `rag` → `rag`; `full` → `full`; empty → `full`; `fast`/`balanced`/garbage → explicit rejection.
- **Verification:**
  - [x] Tests pass: `go test ./cmd/orchestrator/... -run TestNormalizeProfile`
  - [x] Regression: `go test ./cmd/orchestrator/...` green
- **Dependencies:** 1.1
- **Files likely touched:** `cmd/orchestrator/main.go`
- **Spec**: processing-profiles (Profile set)

### Task 1.3 — RED: batch per-document validation
- **Description:** Failing Go test for per-document profile validation: invalid doc → `rejected[]` entry, siblings unaffected; all-invalid → 400. Tested via a pure planner `planBatchProfiles()` because the handler uses package-global concrete `redisInst`/`mqBroker` (not injectable); the planner is the real decision path the handler calls.
- **Acceptance criteria:**
  - [x] `TestPlanBatchProfiles_*` pins partial rejection, inheritance, override and the all-invalid condition.
- **Verification:**
  - [x] Tests pass: `go test ./cmd/orchestrator/handlers/ -run TestPlanBatchProfiles`
- **Dependencies:** 1.2
- **Files likely touched:** `cmd/orchestrator/handlers/batch_test.go`
- **Spec**: batch-ingestion (MODIFIED) — invalid profile rollback

### Task 1.4 — GREEN: per-doc `Profile` in batch
- **Description:** Added `Profile` to `BatchDocument` and `BatchRequest` (batch-level default), `RejectedDocument` + `BatchResponse.Rejected`; `CreateBatchHandler` calls `planBatchProfiles()`, validates the batch profile (400 `invalid_profile`), rejects invalid docs into `rejected[]` with index/filename/reason, excludes them from job creation, and 400s when every document is invalid. Jobs now carry `Profile` (Redis + `JobMessage`). Base normalizer moved to `internal/models.NormalizeProfile` (shared by package `main` and `handlers`).
- **Acceptance criteria:**
  - [x] Mixed `rag`/`full` batch accepted; omitted profile inherits batch profile.
  - [x] Invalid doc reported in `rejected[]` with a clear error; siblings proceed.
  - [x] All-invalid → HTTP 400.
- **Verification:**
  - [x] Tests pass: `go test ./cmd/orchestrator/...`
  - [x] Regression: `make test` green (full Go suite)
- **Dependencies:** 1.3
- **Files likely touched:** `cmd/orchestrator/handlers/batch_models.go`, `cmd/orchestrator/handlers/batch.go`, `internal/models/profile.go`
- **Spec**: batch-ingestion (MODIFIED); D8

### Task 1.5 — RED: type×profile resolver matrix
- **Description:** Failing pytest table-driven test over 4 types × 2 profiles: entry selection, downstream composition, chunking capability, spreadsheet `rag` → no entities step, spreadsheet short-circuit removed.
- **Acceptance criteria:**
  - [ ] `test_pipeline_config.py` covers the full matrix and the removed override.
- **Verification:**
  - [ ] Tests pass: `pytest pkg/worker_common/tests/test_pipeline_config.py -v`
- **Dependencies:** 1.2
- **Files likely touched:** `pkg/worker_common/tests/test_pipeline_config.py`
- **Spec**: pipeline-dag-declaration (MODIFIED) — type×profile composition

### Task 1.6 — GREEN: `pipeline.json` v2 + resolver
- **Description:** Update `configs/pipeline.json` to v2 (`types` with `entry` + `capabilities:[chunking]`, `profiles: rag|full`, drop the spreadsheet pipeline short-circuit); update `queues_for`/`steps_for` to take `entry`, `features`, `profile`.
- **Acceptance criteria:**
  - [ ] Resolver returns `embeddings`+`inferences` and no `entities`/`metadata` for `rag`.
  - [ ] Spreadsheet honors the profile (no override).
  - [ ] No new queues introduced → queue-arg parity unchanged.
  - [ ] Sweep worker-side `profile` defaults (`balanced` fallbacks in extraction/audio/image/completion workers and the resolver signature) to `full`, so no legacy `balanced` default can resurrect the old pipeline.
- **Verification:**
  - [ ] Tests pass: `pytest pkg/worker_common/tests/test_pipeline_config.py -v`
  - [ ] Tests pass: `pytest pkg/worker_common/tests/test_queue_declaration_sites.py -v`
- **Dependencies:** 1.5
- **Files likely touched:** `configs/pipeline.json`, `pkg/worker_common/pipeline_config.py`
- **Spec**: pipeline-dag-declaration (MODIFIED); D2

### Task 1.7 — GREEN: `JobResults` media fields
- **Description:** Add `ImageOcrText`, `ImageDescription *string`, `MetadataImage`, `MetadataAudio`, `AudioSegments` to `JobResults`; remove `SchemaVersion`.
- **Acceptance criteria:**
  - [ ] Struct compiles and serializes with the new fields (`image_description` nullable).
  - [ ] `SchemaVersion` no longer present in the result struct.
- **Verification:**
  - [ ] Build succeeds: `make build-orchestrator`
  - [ ] Tests pass: `make test`
- **Dependencies:** None (independent of 1.5, but same PR)
- **Files likely touched:** `internal/models/job.go`
- **Spec**: job-result-format (MODIFIED)

---

## W2 — Inference dispatch decoupling + `INFERENCE_*` env (PR 2)

**Unit acceptance (`2.7`)**: a `rag` document job (no entities) produces inferences; a single
inference env source exists (no `LLM_URL`/`LLM_MODEL`).
**Rollback boundary**: extraction-worker, entities-worker, audio-worker, image-worker, inference_worker.py.

### Task 2.1 — RED: dispatch ownership + counter race
- **Description:** Failing pytest: extraction fan-out publishes inference chunks with `inferences:remaining`/`:total` set **before** publish.
- **Acceptance criteria:**
  - [ ] Test asserts counters exist before the publish call and fails on the current code.
- **Verification:**
  - [ ] Tests pass: `pytest cmd/extraction-worker/tests/test_dispatch.py -v`
- **Dependencies:** 1.6
- **Files likely touched:** `cmd/extraction-worker/tests/test_dispatch.py`
- **Spec**: processing-profiles (Inference dispatch decoupled)

### Task 2.2 — GREEN: move dispatch to extraction fan-out
- **Description:** Move inference dispatch from `entities_worker.py` to `cmd/extraction-worker/worker.py` fan-out, preserving the counter-before-publish race fix.
- **Acceptance criteria:**
  - [ ] `rag` document job produces inference chunks without an entities step.
  - [ ] Counters set before publish (race fix intact).
- **Verification:**
  - [ ] Tests pass: `pytest cmd/extraction-worker/tests/test_dispatch.py -v`
- **Dependencies:** 2.1
- **Files likely touched:** `cmd/extraction-worker/worker.py`
- **Spec**: processing-profiles (Inference dispatch decoupled); D3

### Task 2.3 — GREEN: audio/image own inference dispatch
- **Description:** Apply the same dispatch pattern to `audio-worker` and `image-worker`.
- **Acceptance criteria:**
  - [ ] Audio and image jobs publish inference chunks from their own fan-out.
- **Verification:**
  - [ ] Tests pass: `pytest cmd/audio-worker/tests/ cmd/image-worker/tests/ -v`
- **Dependencies:** 2.2
- **Files likely touched:** `cmd/audio-worker/worker.py`, `cmd/image-worker/worker.py`
- **Spec**: processing-profiles (Inference dispatch decoupled); D3

### Task 2.4 — GREEN: remove dispatch from entities-worker
- **Description:** Remove the inference dispatch coupling from `entities_worker.py` (entities no longer triggers inferences).
- **Acceptance criteria:**
  - [ ] `entities_worker.py` has no inference-dispatch code path.
  - [ ] Entities still publish their own result step.
- **Verification:**
  - [ ] Tests pass: `pytest cmd/entities-worker/tests/ -v`
- **Dependencies:** 2.3
- **Files likely touched:** `cmd/entities-worker/entities_worker.py`
- **Spec**: processing-profiles (Inference dispatch decoupled); D3

### Task 2.5 — RED: inference env contract
- **Description:** Failing pytest: inference worker reads `INFERENCE_BASE_URL`/`INFERENCE_MODEL` only; unset → startup error.
- **Acceptance criteria:**
  - [ ] Test asserts no fallback to `LLM_URL`/`LLM_MODEL` and an explicit failure when unset.
- **Verification:**
  - [ ] Tests pass: `pytest cmd/inference-worker/tests/test_config.py -v`
- **Dependencies:** None (same PR)
- **Files likely touched:** `cmd/inference-worker/tests/test_config.py`
- **Spec**: Cross-cutting — Inference LLM env separation

### Task 2.6 — GREEN: migrate inference worker env
- **Description:** Migrate `inference_worker.py` from `LLM_URL`/`LLM_MODEL` to `INFERENCE_BASE_URL`/`INFERENCE_MODEL`; no fallback.
- **Acceptance criteria:**
  - [ ] Worker starts only with the new env; unset → clear startup error, inferences disabled.
- **Verification:**
  - [ ] Tests pass: `pytest cmd/inference-worker/tests/ -v`
- **Dependencies:** 2.5
- **Files likely touched:** `cmd/inference-worker/inference_worker.py`
- **Spec**: Cross-cutting — Inference LLM env separation; D6

---

## W3 — Image OCR + description split (PR 3)

**Unit acceptance (`3.7`)**: `image_description` is a single field never embedded; `full`
returns a non-null description; `rag` returns null; media-key TTL asserts pass.
**Rollback boundary**: image_client, image-analyzer, image-worker, job_keys.

### Task 3.1 — RED: profile-driven image calls
- **Description:** Failing pytest: image `full` calls `/analyze` + `/describe`; image `rag` calls `/analyze` only.
- **Acceptance criteria:**
  - [ ] Test pins the two call sets and fails on current code.
- **Verification:**
  - [ ] Tests pass: `pytest cmd/image-worker/tests/test_image_worker.py -v`
- **Dependencies:** 1.6
- **Files likely touched:** `cmd/image-worker/tests/test_image_worker.py`
- **Spec**: image-ocr-description (ADDED) — Profile-driven image capability

### Task 3.2 — GREEN: `describe()` in image client
- **Description:** Add `describe()` to `pkg/image_client/client.py` calling the new `/describe` endpoint.
- **Acceptance criteria:**
  - [ ] Client exposes `describe()` with the configured endpoint/timeout.
- **Verification:**
  - [ ] Tests pass: `pytest cmd/image-worker/tests/ -v`
- **Dependencies:** 3.1
- **Files likely touched:** `pkg/image_client/client.py`
- **Spec**: image-ocr-description (ADDED) — distinct endpoints

### Task 3.3 — GREEN: `/describe` route in image-analyzer
- **Description:** Add `/describe` to `deploy/docker/image-analyzer/app/main.py` (LLM vision description, no OCR).
- **Acceptance criteria:**
  - [ ] `/analyze` stays OCR-only; `/describe` returns a semantic description and never OCR text.
- **Verification:**
  - [ ] Manual check: `curl -F file=@x.jpg .../describe` returns a description payload
- **Dependencies:** 3.2
- **Files likely touched:** `deploy/docker/image-analyzer/app/main.py`
- **Spec**: image-ocr-description (ADDED); D5

### Task 3.4 — GREEN: image-worker profile logic + single-field description
- **Description:** `image-worker/worker.py`: `full` stores the description in `:metadata:image.description` (single field, never chunked/embedded); `rag` skips description. OCR text remains the RAG field.
- **Acceptance criteria:**
  - [ ] No `:image_description` key is written.
  - [ ] Description text never appears in chunks/embeddings.
- **Verification:**
  - [ ] Tests pass: `pytest cmd/image-worker/tests/ -v`
- **Dependencies:** 3.3
- **Files likely touched:** `cmd/image-worker/worker.py`
- **Spec**: image-ocr-description (ADDED); D5

### Task 3.5 — RED: media-key TTL
- **Description:** Failing pytest: media keys (`:metadata:image`, `:metadata:audio`, `:audio_segments`) are written with `setex(JOB_TTL_SECONDS=86400)`.
- **Acceptance criteria:**
  - [ ] Test asserts the TTL on write for each key.
- **Verification:**
  - [ ] Tests pass: `pytest pkg/worker_common/tests/test_job_keys.py -v`
- **Dependencies:** 3.4
- **Files likely touched:** `pkg/worker_common/tests/test_job_keys.py`
- **Spec**: job-result-format (MODIFIED); D7

### Task 3.6 — GREEN: `set_job_key()` TTL helper
- **Description:** Create `pkg/worker_common/job_keys.py` with `set_job_key()` and use it for `:metadata:image`, `:metadata:audio`, `:audio_segments`.
- **Acceptance criteria:**
  - [ ] All three media keys use the helper with TTL 86400.
- **Verification:**
  - [ ] Tests pass: `pytest pkg/worker_common/tests/test_job_keys.py -v`
- **Dependencies:** 3.5
- **Files likely touched:** `pkg/worker_common/job_keys.py`
- **Spec**: job-result-format (MODIFIED); D7

---

## W4 — Remote embeddings client + cutover (PR 4)

**Unit acceptance (`4.6`)**: identical text twice → one HTTP call; partial degradation →
`embeddings_degraded: true`; no local fallback remains.
**Rollback boundary**: embeddings_client.py, embeddings-worker, inference_embeddings.py.
**Prerequisite**: reachable dev endpoint at `EMBEDDINGS_BASE_URL` (1024-dim contract test).

### Task 4.1 — RED: cache-by-content-hash
- **Description:** Failing pytest: identical text twice → exactly one HTTP call (cache hit by `sha256(model+text)`).
- **Acceptance criteria:**
  - [ ] Test counts HTTP calls with a mock transport + fake Redis.
- **Verification:**
  - [ ] Tests pass: `pytest pkg/worker_common/tests/test_embeddings_client.py -v`
- **Dependencies:** 1.6
- **Files likely touched:** `pkg/worker_common/tests/test_embeddings_client.py`
- **Spec**: remote-embeddings (ADDED) — success path

### Task 4.2 — GREEN: `EmbeddingsClient`
- **Description:** Create `pkg/worker_common/embeddings_client.py`: `embed()`/`embed_batch()` over `POST /v1/embeddings`, Redis cache `emb:{hash}` with TTL `EMBEDDINGS_CACHE_TTL_SECONDS` (7d), batching, timeouts, retries, error mapping.
- **Acceptance criteria:**
  - [ ] 1024-dim vectors returned; cache hit skips HTTP.
  - [ ] Config from `EMBEDDINGS_BASE_URL`/`EMBEDDINGS_MODEL`.
- **Verification:**
  - [ ] Tests pass: `pytest pkg/worker_common/tests/test_embeddings_client.py -v`
- **Dependencies:** 4.1
- **Files likely touched:** `pkg/worker_common/embeddings_client.py`
- **Spec**: remote-embeddings (ADDED); D4

### Task 4.3 — RED: degraded vs total failure
- **Description:** Failing pytest: partial batch failure → `embeddings_degraded: true` with successful vectors; total failure → job fails (no fallback).
- **Acceptance criteria:**
  - [ ] Both paths pinned by tests.
- **Verification:**
  - [ ] Tests pass: `pytest pkg/worker_common/tests/test_embeddings_client.py -v`
- **Dependencies:** 4.2
- **Files likely touched:** `pkg/worker_common/tests/test_embeddings_client.py`
- **Spec**: remote-embeddings (ADDED) — failure/degraded paths

### Task 4.4 — GREEN: degraded path
- **Description:** Implement the degraded path: partial failure logs + returns successful vectors + sets `embeddings_degraded`; total failure raises with a clear message.
- **Acceptance criteria:**
  - [ ] Flag surfaces in results; total failure is explicit.
- **Verification:**
  - [ ] Tests pass: `pytest pkg/worker_common/tests/test_embeddings_client.py -v`
- **Dependencies:** 4.3
- **Files likely touched:** `pkg/worker_common/embeddings_client.py`
- **Spec**: remote-embeddings (ADDED); D4

### Task 4.5 — GREEN: cutover the three sites
- **Description:** Replace local bge-m3 loading in `cmd/embeddings-worker/`, `pkg/worker_common/inference_embeddings.py` and `cmd/completion-worker/` with `EmbeddingsClient`.
- **Acceptance criteria:**
  - [ ] No local model-loading path remains; no local fallback.
  - [ ] Vector dimension parity (1024) verified against the dev endpoint.
- **Verification:**
  - [ ] Tests pass: `pytest cmd/embeddings-worker/tests/ -v`
  - [ ] Contract check: 1024-dim vector returned by the dev endpoint
- **Dependencies:** 4.4
- **Files likely touched:** `cmd/embeddings-worker/embeddings_worker.py`, `pkg/worker_common/inference_embeddings.py`, `cmd/completion-worker/completion_worker.py`
- **Spec**: remote-embeddings (ADDED) — replace all three local paths; D4

---

## W5 — Completion media fields + docs + client (PR 5)

**Unit acceptance (`5.7`)**: mixed-profile batch E2E passes; docs updated; client sends
`profile`; queue parity green.
**Rollback boundary**: completion_worker.py, tools/client, docs.

### Task 5.1 — RED: final JSON media fields
- **Description:** Failing pytest: final JSON includes `image_ocr_text`, `image_description` (null when not requested), `metadata:image`, `metadata:audio`, `audio_segments`.
- **Acceptance criteria:**
  - [ ] Test covers image `full`, image `rag`, audio and document outputs.
- **Verification:**
  - [ ] Tests pass: `pytest cmd/completion-worker/tests/test_completion.py -v`
- **Dependencies:** 1.7, 3.6, 4.5
- **Files likely touched:** `cmd/completion-worker/tests/test_completion.py`
- **Spec**: job-result-format (MODIFIED)

### Task 5.2 — GREEN: read media keys in completion
- **Description:** Update `completion_worker.py` to read the media keys from Redis and populate the `JobResults` media fields; text remains the common RAG field.
- **Acceptance criteria:**
  - [ ] `image_description` is null (not omitted) when not requested.
  - [ ] Facets correct for image/audio/document.
- **Verification:**
  - [ ] Tests pass: `pytest cmd/completion-worker/tests/ -v`
- **Dependencies:** 5.1
- **Files likely touched:** `cmd/completion-worker/completion_worker.py`
- **Spec**: job-result-format (MODIFIED)

### Task 5.3 — GREEN: client sends `profile`
- **Description:** Update `tools/client/main.go` to send `profile` on job submission.
- **Acceptance criteria:**
  - [ ] Client accepts `rag|full` and includes it in the request; default `full`.
- **Verification:**
  - [ ] Build succeeds: `make build-client`
  - [ ] Manual check: client submits a job carrying a valid `profile`
- **Dependencies:** 1.4
- **Files likely touched:** `tools/client/main.go`
- **Spec**: Cross-cutting — Docs and test client

### Task 5.4 — GREEN: docs update
- **Description:** Update `docs/API.md`, `docs/CONFIGURATION.md`, `docs/MODELS.md`: `rag|full` profiles, breaking rename (`fast`/`balanced` removed), `INFERENCE_BASE_URL`/`INFERENCE_MODEL`, `EMBEDDINGS_BASE_URL`/`EMBEDDINGS_MODEL`, `OCR_*`/`IMAGE_*`, media fields.
- **Acceptance criteria:**
  - [ ] Docs describe profiles, per-file profile, media fields and all new env vars.
- **Verification:**
  - [ ] Manual check: each env var appears in `CONFIGURATION.md`; no stale `LLM_URL`/`fast`/`balanced` references
- **Dependencies:** 5.2
- **Files likely touched:** `docs/API.md`, `docs/CONFIGURATION.md`, `docs/MODELS.md`
- **Spec**: Cross-cutting — Docs and test client

### Task 5.5 — Verify queue parity
- **Description:** Confirm no new queues were added and Go↔Python declaration parity holds.
- **Acceptance criteria:**
  - [ ] Parity suite green with no new queue names.
- **Verification:**
  - [ ] Tests pass: `pytest pkg/worker_common/tests/test_queue_declaration_sites.py -v`
- **Dependencies:** 5.2
- **Files likely touched:** — (verification only)
- **Spec**: Cross-cutting — Migration runbook

### Task 5.6 — E2E: mixed-profile batch
- **Description:** End-to-end run with the test client: one batch containing a `rag` and a `full` document produces per-file stage sets per the type×profile matrix.
- **Acceptance criteria:**
  - [ ] Each document ends with the stages its profile dictates; no cross-contamination.
  - [ ] Full suite green (Go + Python).
- **Verification:**
  - [ ] E2E: mixed-profile batch via `bin/client`
  - [ ] Tests pass: `make test`
  - [ ] Tests pass: `make test-python`
- **Dependencies:** 5.1–5.5
- **Files likely touched:** — (verification only)
- **Spec**: Success Criteria — mixed-profile batch

---

## Progress Log

| Unit | PR | Status |
|---|---|---|
| W1 | PR 1 | 🟡 1.1–1.4 done (admission + batch profiles) — resolver v2 pending (1.6) |
| W2 | PR 2 | ⬜ blocked by W1 |
| W3 | PR 3 | ⬜ blocked by W1 |
| W4 | PR 4 | ⬜ blocked by W1 (endpoint prerequisite) |
| W5 | PR 5 | ⬜ blocked by W1–W4 |
