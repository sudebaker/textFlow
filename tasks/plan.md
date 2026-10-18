# Plan: Per-File Processing Profiles (`perfiles-por-fichero`)

**Spec**: `SPEC.md` (repo root)
**Delivery**: chained PRs, stacked-to-main — W1 → W2 → W3 → W4 → W5
**Risk**: ~900–1100 changed lines against a 400-line review budget → split mandatory

## Technical Approach

Declarative extension of `configs/pipeline.json` to schema `v2`. The DAG stays declarative
(the D4 invariant: the pipeline lives in `pipeline.json`, **not** in the Go orchestrator).

Profiles become **capability sets**:

- `rag` = entry + chunking + embeddings + inferences
- `full` = `rag` + entities + metadata (+ `description` for images), default

The **file type selects the entry stage**; the **profile composes** applicable downstream
stages (compose, never override). The spreadsheet short-circuit is removed. Inference
dispatch moves out of entities-worker into the entry-stage fan-out (extraction/audio/image),
preserving the counter-before-publish race fix. Local bge-m3 loading is replaced by a remote
OpenAI-compatible `EmbeddingsClient` at three sites. image-analyzer splits OCR (`/analyze`)
from description (`/describe`). Completion emits explicit media fields.

## Architecture Decisions

| # | Decision | Choice |
|---|---|---|
| D1 | Profile set | `rag\|full`, default `full` (breaking rename of `fast\|balanced`) |
| D2 | Chunking location | Not a stage/queue; runs inside each entry stage. Schema declares `chunking` as an entry-stage capability → no new queue, no parity impact |
| D3 | Inference dispatch | Move to extraction/audio/image fan-out; counters set before publish; entities-worker decoupled; inference-worker unchanged |
| D4 | Remote embeddings | `EmbeddingsClient` → `/v1/embeddings`, no local fallback; `EMBEDDINGS_BASE_URL`/`EMBEDDINGS_MODEL` |
| D5 | Image description | Single field `:metadata:image.description` → `image_description`; never chunked/embedded. `full` calls `/analyze`+`/describe`; `rag` OCR only |
| D6 | Inference LLM env | `INFERENCE_BASE_URL`/`INFERENCE_MODEL` ONLY; `LLM_URL`/`LLM_MODEL` removed; unset → startup error |
| D7 | Redis TTLs | Python media keys use `setex(JOB_TTL_SECONDS=86400)` via shared `set_job_key()`; no `:image_description` key |
| D8 | Batch invalid profile | Validate per-doc at admission; invalid docs → `BatchResponse.rejected[]`; siblings unaffected; all-invalid → 400 |

### D2 composition matrix

| Type | Entry (always chunks) | `rag` downstream | `full` downstream |
|---|---|---|---|
| document | extraction | embeddings, inferences | entities, metadata, inferences |
| spreadsheet | extraction | embeddings, inferences | entities, metadata, inferences |
| audio | audio | embeddings, inferences | entities, metadata, inferences |
| image | image | embeddings, inferences | entities, metadata, inferences, description |

## Dependency Graph

```
W1 pipeline.json v2 + resolver + normalizeProfile + per-doc profile + JobResults fields
 │
 ├── W2 inference dispatch → entry fan-out + INFERENCE_* env
 │
 ├── W3 image OCR/description split + job_keys TTL helper
 │
 └── W4 embeddings_client + cache + degraded path + cutover 3 sites
      │
      └── W5 completion media fields + docs + client   (needs W1 structs, W3 keys, W4 client)
```

W2/W3/W4 are independent of each other but all depend on W1's schema/resolver. W5 depends on
all prior units. Sequential stacked-to-main keeps each PR reviewable.

## File Changes

| File | Action | Unit |
|---|---|---|
| `configs/pipeline.json` | Modify → v2 (`rag\|full`, entry+chunking capability, drop short-circuit) | W1 |
| `pkg/worker_common/pipeline_config.py` | Modify → `queues_for/steps_for(entry, features, profile)` | W1 |
| `cmd/orchestrator/main.go` | Modify → `normalizeProfile` (rag\|full, default full) | W1 |
| `cmd/orchestrator/handlers/batch_models.go`, `batch.go` | Modify → per-doc `Profile`, validation, `rejected[]` | W1 |
| `internal/models/job.go` | Modify → media fields; remove `SchemaVersion` | W1 |
| `cmd/extraction-worker/worker.py` | Modify → owns inference dispatch | W2 |
| `cmd/{audio,image}-worker/*.py` | Modify → own inference dispatch | W2 |
| `cmd/entities-worker/entities_worker.py` | Modify → remove dispatch coupling | W2 |
| `cmd/inference-worker/inference_worker.py` | Modify → `INFERENCE_BASE_URL`/`INFERENCE_MODEL` | W2 |
| `pkg/image_client/client.py` | Modify → `describe()` | W3 |
| `deploy/docker/image-analyzer/app/main.py` | Modify → `/describe` route | W3 |
| `cmd/image-worker/worker.py` | Modify → profile-driven OCR/description, media TTL | W3 |
| `pkg/worker_common/job_keys.py` | Create → `set_job_key()` TTL helper | W3 |
| `pkg/worker_common/embeddings_client.py` | Create → `/v1/embeddings` + cache + degraded | W4 |
| `cmd/embeddings-worker/`, `pkg/worker_common/inference_embeddings.py`, `cmd/completion-worker/` | Modify → remote client, no local load | W4 |
| `cmd/completion-worker/completion_worker.py` | Modify → read media keys, populate `JobResults` | W5 |
| `tools/client/main.go` | Modify → send `profile` | W5 |
| `docs/API.md`, `docs/CONFIGURATION.md`, `docs/MODELS.md` | Modify → rag\|full, breaking rename, new envs | W5 |

## Interfaces / Contracts

### `configs/pipeline.json` (v2)

```json
{
  "version": "v2",
  "types": {
    "document":    {"entry": "extraction", "capabilities": ["chunking"]},
    "spreadsheet": {"entry": "extraction", "capabilities": ["chunking"]},
    "audio":       {"entry": "audio",      "capabilities": ["chunking"]},
    "image":       {"entry": "image",      "capabilities": ["chunking"]}
  },
  "profiles": {
    "rag":  {"downstream": ["embeddings", "inferences"]},
    "full": {"downstream": ["entities", "metadata", "inferences"], "image_description": true}
  },
  "rules": {"audio_replaces_extraction": true, "image_replaces_extraction": true}
}
```

### Resolver + embeddings client

```python
def queues_for(self, *, entry: str, features: List[str], profile: str = "full") -> List[str]: ...
def steps_for(self, *, entry: str, features: List[str], profile: str = "full") -> Set[str]: ...

class EmbeddingsClient:
    def embed(self, texts): ...                    # cached by sha256(model+text)
    def embed_batch(self, texts, batch_size=32): ...
```

Cache key `emb:{sha256(model+text)}` → JSON vector, TTL `EMBEDDINGS_CACHE_TTL_SECONDS` (7d).
Failure policy: total/transport failure → job fails (no fallback); partial batch failure →
successful vectors returned + `embeddings_degraded: true`.

## Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Inference dispatch race | Med | Set `inferences:remaining/:total` before publish (existing pattern); integration test |
| Default `full` runs inferences for existing clients | High | Documented breaking change; `rag` opt-out; client updated |
| Remote embeddings contract (1024-dim bge-m3) | Med | Contract test against dev endpoint before cutover (W4) |
| Go↔Python queue parity | Low | No new queues; parity test re-run in W1 and W5 |
| Review budget >400 lines | High | 5 chained PRs stacked-to-main |
| Spreadsheet chunk quality regression (short-circuit removed) | Low–Med | Reuse entry-stage chunker; AC asserts non-empty chunks+embeddings; quality tuning deferred |

## Migration / Rollout

Drain + big-bang (no dual-run):

1. Stop admission (pause `POST /v1/documents` / LB).
2. Drain: wait for `ZCard active_jobs == 0` (`JobTimeout=60m` bounds worst case).
3. Deploy new images (orchestrator + workers + configs/pipeline.json v2).
4. Resume admission; verify with a test job (spreadsheet + `full` + `features=["inferences"]`).

Rollback: drain, redeploy previous images, resume. No new queues → RabbitMQ `PRECONDITION_FAILED`
risk is nil (queue args unchanged).

## Verification Checkpoints

| Checkpoint | Command | Gate |
|---|---|---|
| W1 | `go test ./cmd/orchestrator/... -run TestBatch` + `pytest pkg/worker_common/tests/test_pipeline_config.py -v` + `pytest pkg/worker_common/tests/test_queue_declaration_sites.py -v` | spreadsheet `rag` → chunks+embeddings, no entities; batch invalid → `rejected[]`; all-invalid → 400 |
| W2 | `pytest cmd/extraction-worker/tests/ -v` + `pytest cmd/inference-worker/tests/ -v` | `rag` job produces inferences; single inference env source |
| W3 | `pytest cmd/image-worker/tests/ -v` | description single-field, never embedded; TTL asserts |
| W4 | `pytest pkg/worker_common/tests/test_embeddings_client.py -v` | identical text twice → one HTTP call; degraded partial → flag; no local fallback |
| W5 | `pytest cmd/completion-worker/tests/ -v` + `make test` + `make test-python` | mixed-profile batch E2E; docs + client updated; parity green |

Review gates between phases: SPEC → plan → tasks → implement (human validates each).
