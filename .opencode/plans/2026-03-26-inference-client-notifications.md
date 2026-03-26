# Inference Client Notifications — Fix 3 Gaps

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix three independent gaps so that the Go orchestrator correctly deserialises and exposes the per-chunk micro-inference data produced by Python workers, and clients can see inference progress during polling.

**Architecture:** The Python `inference-worker` now produces grouped-by-chunk JSON (`[{chunk_id, inferences:[{text, confidence, entities}]}]`) but the Go `MicroInference` struct still expects the old flat `{fact, confidence, source}` shape. Fix the struct, add the missing status constant, expose `steps` to polling clients during processing, and publish per-chunk progress events from Python. All changes are additive — existing jobs without the `inferences` feature are unaffected.

**Tech Stack:** Go 1.22 (models, redis client, orchestrator handler), Python 3.11 (inference-worker, events_python), miniredis (Go tests), pytest + unittest.mock (Python tests).

---

## Files Modified

| File | Change |
|------|--------|
| `internal/models/job.go` | Fix `MicroInference`, add `ChunkInferences`, add `StatusInferences`, add `Steps` to `GetJobResponse` |
| `internal/redis/client_test.go` | Add tests for `GetJobResults` round-trip with new struct shape |
| `cmd/orchestrator/main.go` | `getJobHandler`: populate `Steps` regardless of status |
| `pkg/events_python.py` | Add `publish_job_inference_chunk_progress` helper |
| `cmd/inference-worker/worker.py` | Call new helper after each chunk completes (non-last chunks) |
| `cmd/inference-worker/tests/test_inference_worker.py` | Update stale tests; add tests for chunk progress publishing |

---

## Task 1: Fix Go struct `MicroInference` + add `ChunkInferences` + `StatusInferences`

**Files:**
- Modify: `internal/models/job.go`

### Context

Current struct (wrong):
```go
type MicroInference struct {
    Fact       string  `json:"fact"`
    Confidence float32 `json:"confidence"`
    Source     string  `json:"source"`
}
// JobResults.MicroInferences []MicroInference  ← flat list
```

Python now writes to Redis (correct):
```json
[{"chunk_id": 0, "inferences": [{"text":"...", "confidence":0.85, "entities":["Juan"]}]}]
```

- [ ] **Step 1.1: Update `MicroInference` struct fields**

Replace:
```go
type MicroInference struct {
    Fact       string  `json:"fact"`
    Confidence float32 `json:"confidence"`
    Source     string  `json:"source"`
}
```
With:
```go
type MicroInference struct {
    Text       string   `json:"text"`
    Confidence float32  `json:"confidence"`
    Entities   []string `json:"entities,omitempty"`
}
```

- [ ] **Step 1.2: Add `ChunkInferences` type**

Add immediately after `MicroInference`:
```go
type ChunkInferences struct {
    ChunkID    interface{}      `json:"chunk_id"` // int or string from Python
    Inferences []MicroInference `json:"inferences"`
}
```

Note: `chunk_id` in Python is set as the raw value from the RabbitMQ message which can be an int or string depending on the worker that emits the task. Using `interface{}` avoids silent zero-value on type mismatch.

- [ ] **Step 1.3: Update `JobResults.MicroInferences` field**

Change:
```go
MicroInferences      []MicroInference            `json:"micro_inferences,omitempty"`
```
To:
```go
MicroInferences      []ChunkInferences           `json:"micro_inferences,omitempty"`
```

- [ ] **Step 1.4: Add `StatusInferences` to the enum**

In the `const` block after `StatusEntities`:
```go
StatusInferences JobStatus = "inferences"
```

- [ ] **Step 1.5: Add `Steps` field to `GetJobResponse`**

```go
type GetJobResponse struct {
    JobID     string            `json:"job_id"`
    Status    JobStatus         `json:"status"`
    Steps     map[string]string `json:"steps,omitempty"`
    Results   *JobResults       `json:"results,omitempty"`
    Error     string            `json:"error,omitempty"`
    CreatedAt time.Time         `json:"created_at"`
}
```

- [ ] **Step 1.6: Verify the file compiles**

```bash
go build ./internal/models/...
```
Expected: no errors.

- [ ] **Step 1.7: Commit**

```bash
git add internal/models/job.go
git commit -m "fix: update MicroInference struct and add ChunkInferences, StatusInferences, Steps in GetJobResponse"
```

---

## Task 2: Go test — `GetJobResults` round-trip with new struct shape

**Files:**
- Modify: `internal/redis/client_test.go`

This ensures the JSON stored by Python is correctly deserialised by Go.

- [ ] **Step 2.1: Add test `TestGetJobResults_MicroInferences`**

Append to `internal/redis/client_test.go`:
```go
func TestGetJobResults_MicroInferences(t *testing.T) {
    mr, client := setupTestRedis(t)
    defer mr.Close()
    defer client.Close()

    ctx := context.Background()
    jobID := "test-job-mi"

    // Simulate what Python completion-worker writes to Redis
    pythonJSON := `{
        "job_id": "test-job-mi",
        "status": "completed",
        "created_at": "2026-01-01T00:00:00",
        "completed_at": "2026-01-01T00:01:00",
        "chunks": [],
        "entities": [],
        "micro_inferences": [
            {
                "chunk_id": 0,
                "inferences": [
                    {"text": "Property value is 500000 EUR", "confidence": 0.95, "entities": ["500000 EUR"]}
                ]
            },
            {
                "chunk_id": 1,
                "inferences": []
            }
        ]
    }`

    key := client.key("job", jobID, "results")
    err := client.GetClient().Set(ctx, key, pythonJSON, time.Hour).Err()
    require.NoError(t, err)

    results, err := client.GetJobResults(ctx, jobID)
    require.NoError(t, err)
    require.NotNil(t, results)

    assert.Equal(t, 2, len(results.MicroInferences))
    assert.Equal(t, 1, len(results.MicroInferences[0].Inferences))
    assert.Equal(t, "Property value is 500000 EUR", results.MicroInferences[0].Inferences[0].Text)
    assert.InDelta(t, 0.95, results.MicroInferences[0].Inferences[0].Confidence, 0.001)
    assert.Equal(t, []string{"500000 EUR"}, results.MicroInferences[0].Inferences[0].Entities)
    assert.Equal(t, 0, len(results.MicroInferences[1].Inferences))
}
```

Note: `client.key(...)` is unexported but the test is in the same package (`package redis`), so this works.

- [ ] **Step 2.2: Run the test to make sure it passes**

```bash
go test -v ./internal/redis/... -run TestGetJobResults_MicroInferences
```
Expected: PASS.

- [ ] **Step 2.3: Run full Go test suite**

```bash
make test
```
Expected: all tests pass.

- [ ] **Step 2.4: Commit**

```bash
git add internal/redis/client_test.go
git commit -m "test: add GetJobResults round-trip test for per-chunk MicroInferences struct"
```

---

## Task 3: Populate `Steps` in `getJobHandler` for all statuses

**Files:**
- Modify: `cmd/orchestrator/main.go` (around line 454-468, inside `getJobHandler`)

Currently `Results` (and thus per-step progress) is only fetched when `status == completed`. Clients polling during processing get no visibility into which steps are done.

- [ ] **Step 3.1: Fetch `steps` unconditionally in `getJobHandler`**

In `getJobHandler`, after fetching `status` and before building the response, add:

```go
// Get step progress (available at all stages, not just completed)
steps, _ := redis.GetJobSteps(ctx, jobID)
```

- [ ] **Step 3.2: Include `steps` in the response**

Change the `c.JSON` call from:
```go
c.JSON(http.StatusOK, models.GetJobResponse{
    JobID:     jobID,
    Status:    status,
    Results:   results,
    Error:     errorMsg,
    CreatedAt: createdAt,
})
```
To:
```go
c.JSON(http.StatusOK, models.GetJobResponse{
    JobID:     jobID,
    Status:    status,
    Steps:     steps,
    Results:   results,
    Error:     errorMsg,
    CreatedAt: createdAt,
})
```

- [ ] **Step 3.3: Verify build**

```bash
go build ./cmd/orchestrator/...
```
Expected: no errors.

- [ ] **Step 3.4: Run full test suite**

```bash
make test
```
Expected: all tests pass.

- [ ] **Step 3.5: Commit**

```bash
git add cmd/orchestrator/main.go
git commit -m "feat: expose job steps in GET /v1/documents/:id for all statuses"
```

---

## Task 4: Python — per-chunk progress events from inference-worker

**Files:**
- Modify: `pkg/events_python.py`
- Modify: `cmd/inference-worker/worker.py`

Currently, the inference-worker publishes only one progress event (at 80%) after the final chunk is assembled. For N-chunk documents, clients see nothing until all chunks complete. This task adds a per-chunk progress event on every non-last chunk.

- [ ] **Step 4.1: Add `publish_job_inference_chunk_progress` to `EventBus`**

Append to `pkg/events_python.py`:
```python
def publish_job_inference_chunk_progress(
    self,
    job_id: str,
    chunks_done: int,
    chunks_total: int,
) -> None:
    """Publish incremental progress for each inference chunk completed."""
    # Scale from 60% (post-entities) to 79% (just before final assembly at 80%)
    base = 60
    span = 19
    progress = base + int(span * chunks_done / max(chunks_total, 1))
    self.publish_event(
        job_id,
        "job_progress",
        progress=progress,
        status="inferences",
        metadata={"chunks_done": chunks_done, "chunks_total": chunks_total},
    )
```

- [ ] **Step 4.2: Call new helper in `worker.py` on non-last chunks**

In `InferenceWorker.process`, in the `else` branch (currently just `jobs_total.labels(status="chunk_processed").inc()`), add the progress publish before the counter increment.

Replace:
```python
else:
    # Not the last chunk, just continue
    jobs_total.labels(status="chunk_processed").inc()
```
With:
```python
else:
    # Not the last chunk — publish incremental progress so clients see activity
    chunks_done = total_chunks - remaining
    # remaining was already decremented; chunks_done = total - remaining
    self.event_bus.publish_job_inference_chunk_progress(
        job_id,
        chunks_done=chunks_done,
        chunks_total=total_chunks,
    )
    jobs_total.labels(status="chunk_processed").inc()
```

Note: `remaining` is already in scope at this point (result of `decr` call above). `total_chunks` is parsed from the RabbitMQ message at line 149.

- [ ] **Step 4.3: Verify Python syntax**

```bash
python -m py_compile pkg/events_python.py cmd/inference-worker/worker.py
```
Expected: no output (no syntax errors).

- [ ] **Step 4.4: Commit**

```bash
git add pkg/events_python.py cmd/inference-worker/worker.py
git commit -m "feat: publish per-chunk inference progress events from inference-worker"
```

---

## Task 5: Update Python tests

**Files:**
- Modify: `cmd/inference-worker/tests/test_inference_worker.py`

The existing tests are stale — they assert on `fact` and `source` fields that no longer exist in the worker, and they don't test the new chunk-progress path.

- [ ] **Step 5.1: Fix `test_extract_inferences_success` to match new schema**

The mock LLM response must return `text`/`confidence`/`entities` (not `fact`/`source`). Update the test:

```python
def test_extract_inferences_success(self, worker):
    """Test successful inference extraction returns new schema fields"""
    text = "The property has a value of 500,000 EUR and was built in 2010."

    with patch("worker.LLM_URL", "http://localhost:8000"):
        with patch("worker.LLM_MODEL", "test-model"):
            with patch("requests.post") as mock_post:
                mock_response = Mock()
                mock_response.raise_for_status = Mock()
                mock_response.json.return_value = {
                    "choices": [{
                        "text": '[{"text": "Property value is 500000 EUR", "confidence": 0.95, "entities": ["500000 EUR"]}]'
                    }]
                }
                mock_post.return_value = mock_response

                inferences = worker.extract_inferences(
                    chunk_text=text,
                    entities=[],
                    source_type="catastro",
                )

                assert len(inferences) == 1
                assert inferences[0]["text"] == "Property value is 500000 EUR"
                assert inferences[0]["confidence"] == 0.95
                assert inferences[0]["entities"] == ["500000 EUR"]
                # Old fields must NOT be present
                assert "fact" not in inferences[0]
                assert "source" not in inferences[0]
```

- [ ] **Step 5.2: Fix `test_extract_inferences_no_llm_url` signature**

The existing test calls `worker.extract_inferences("Some text")` with a positional string, but the new signature requires keyword args. Update:
```python
def test_extract_inferences_no_llm_url(self, worker):
    with patch("worker.LLM_URL", ""):
        inferences = worker.extract_inferences(
            chunk_text="Some text", entities=[], source_type="generico"
        )
        assert inferences == []
```

- [ ] **Step 5.3: Fix `test_extract_inferences_llm_failure` signature**

```python
def test_extract_inferences_llm_failure(self, worker):
    with patch("worker.LLM_URL", "http://localhost:8000"):
        with patch("worker.LLM_MODEL", "test-model"):
            with patch("requests.post") as mock_post:
                mock_post.side_effect = Exception("Connection failed")
                inferences = worker.extract_inferences(
                    chunk_text="Some text", entities=[], source_type="generico"
                )
                assert inferences == []
```

- [ ] **Step 5.4: Add test for chunk progress publishing**

```python
def test_process_non_last_chunk_publishes_progress(self, worker):
    """Non-last chunk should publish incremental inference progress"""
    worker.redis_client = Mock()
    worker.event_bus = Mock()

    # remaining=1 → not the last chunk
    worker.redis_client.decr.return_value = 1
    worker.redis_client.rpush.return_value = 1
    worker.redis_client.expire.return_value = True

    ch = Mock()
    method = Mock()
    method.delivery_tag = "tag1"

    message = {
        "job_id": "job-123",
        "chunk_id": 0,
        "chunk_text": "Some text about a property.",
        "entities": [],
        "source_type": "generico",
        "total_chunks": 3,
    }

    with patch("worker.LLM_URL", ""):
        worker.process(ch, method, None, json.dumps(message).encode())

    worker.event_bus.publish_job_inference_chunk_progress.assert_called_once_with(
        "job-123",
        chunks_done=2,   # total_chunks(3) - remaining(1)
        chunks_total=3,
    )
    ch.basic_ack.assert_called_once()
```

- [ ] **Step 5.5: Run the test file**

```bash
pytest cmd/inference-worker/tests/test_inference_worker.py -v
```
Expected: all tests PASS.

- [ ] **Step 5.6: Commit**

```bash
git add cmd/inference-worker/tests/test_inference_worker.py
git commit -m "test: update inference-worker tests for new schema and chunk progress"
```

---

## Verification Checklist

After all tasks:

- [ ] `make test` passes (all Go tests green)
- [ ] `pytest cmd/inference-worker/tests/test_inference_worker.py -v` all green
- [ ] `go build ./...` clean
- [ ] `python -m py_compile pkg/events_python.py cmd/inference-worker/worker.py` clean
- [ ] `GET /v1/documents/:id` response during processing includes `"steps": {...}` key
- [ ] `GET /v1/documents/:id` response when completed has `micro_inferences` as list of `{chunk_id, inferences:[{text, confidence, entities}]}`
