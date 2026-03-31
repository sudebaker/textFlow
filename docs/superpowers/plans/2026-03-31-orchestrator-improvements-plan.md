# Orchestrator Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement 6 improvements across 3 phases: entity offsets, per-request webhooks, OpenAPI/Swagger, SSE streaming, batch processing, and embeddings compression.

**Architecture:** 
- Phase 1 (Quick Wins): Extend existing Go models and Python worker to preserve entity offsets; add gin-swagger for API docs; store per-job webhook config in existing Redis :meta hash
- Phase 2 (Features): New SSE endpoint reusing existing EventBus Redis pub/sub; new batch endpoint creating individual jobs grouped by batch_id; completion-worker detects batch completion via Redis counter
- Phase 3 (Optimization): Query param `?compression=gzip` on download endpoint encodes embeddings as base64(gzip(bytes))

**Tech Stack:** Go 1.22, Gin, go-redis, swaggo/gin-swagger, Python 3, compress/gzip

---

## Phase 1: Quick Wins

### Task 1: Entity Offsets in Response

**Files:**
- Modify: `internal/models/job.go:70-76`
- Modify: `cmd/completion-worker/worker.py:223-298`
- Test: `cmd/completion-worker/tests/test_finalize_job.py`
- Test: `internal/redis/client_test.go`

- [ ] **Step 1: Add fields to EntityMinimal struct**

Modify `internal/models/job.go` lines 70-76:
```go
// Antes (líneas 70-76):
type EntityMinimal struct {
    Label      string  `json:"label"`
    Text       string  `json:"text"`
    Confidence float32 `json:"confidence"`
}

// Después:
type EntityMinimal struct {
    Label       string  `json:"label"`
    Text        string  `json:"text"`
    Confidence  float32 `json:"confidence"`
    StartOffset int     `json:"start_offset"`
    EndOffset   int     `json:"end_offset"`
    ChunkID     string  `json:"chunk_id,omitempty"`
}
```

Run: `go build ./internal/models/`
Expected: SUCCESS

- [ ] **Step 2: Modify deduplicate_entities to preserve first match offsets**

In `cmd/completion-worker/worker.py` around line 223-298, the `deduplicate_entities` function strips offsets (see line 240 comment). Change the merge logic (lines 274-283) to preserve `start`, `end`, `chunk_id` from the HIGHEST CONFIDENCE entity (not the first), since that's the current behavior.

```python
# Around line 274-283, change:
# Antes:
if matched_id:
    # Merge: keep highest confidence as representative
    if confidence > result[matched_id].get("confidence", 0):
        result[matched_id] = {
            "label": label,
            "text": text,
            "confidence": confidence,
        }
        norm_index[matched_id] = _normalize(text)

# Después (preserve offsets from highest confidence):
if matched_id:
    if confidence > result[matched_id].get("confidence", 0):
        result[matched_id] = {
            "label": label,
            "text": text,
            "confidence": confidence,
            "start_offset": ent.get("start", 0),
            "end_offset": ent.get("end", 0),
            "chunk_id": ent.get("chunk_id", ""),
        }
        norm_index[matched_id] = _normalize(text)
```

And in the new entity creation (lines 284-292), add the offset fields:
```python
# Around line 286-291, change:
# Antes:
result[eid] = {
    "label": label,
    "text": text,
    "confidence": confidence,
}
# Después:
result[eid] = {
    "label": label,
    "text": text,
    "confidence": confidence,
    "start_offset": ent.get("start", 0),
    "end_offset": ent.get("end", 0),
    "chunk_id": ent.get("chunk_id", ""),
}
```

Run: `pytest cmd/completion-worker/tests/test_finalize_job.py -v`
Expected: Existing tests still pass

- [ ] **Step 3: Add test for entity offsets in output**

In `cmd/completion-worker/tests/test_finalize_job.py`, add a new test after existing tests:
```python
def test_entity_offsets_preserved():
    """EntityMinimal must include start_offset, end_offset, chunk_id from highest confidence match."""
    worker = _make_worker()
    chunks = [{"chunk_id": "chunk_000", "text": "hello world"}]
    entities_raw = [
        {"label": "PER", "text": "Juan", "confidence": 0.8, "chunk_id": "chunk_000", 
         "entity_id": "abc000000001", "start": 0, "end": 4},
        {"label": "PER", "text": "Juan", "confidence": 0.95, "chunk_id": "chunk_001",
         "entity_id": "abc000000001", "start": 10, "end": 14},
    ]

    pipe = MagicMock()
    pipe.execute.return_value = _make_redis_pipeline(chunks, entities_raw)
    worker.redis_client.pipeline.return_value = pipe
    worker.redis_raw.get.return_value = None
    worker.redis_client.set = MagicMock()

    with patch.object(worker, "save_results_to_file"), patch.object(worker, "send_webhook"), \
         patch.object(worker.event_bus, "publish_job_completed"):
        worker.finalize_job("job_abc")

    saved = _get_results(worker)
    entity = saved["entities"]["abc000000001"]
    assert entity["start_offset"] == 10, "Should use offset from highest confidence"
    assert entity["end_offset"] == 14
    assert entity["chunk_id"] == "chunk_001"
```

Run: `pytest cmd/completion-worker/tests/test_finalize_job.py::test_entity_offsets_preserved -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add internal/models/job.go cmd/completion-worker/worker.py cmd/completion-worker/tests/test_finalize_job.py
git commit -m "feat: add start_offset, end_offset, chunk_id to EntityMinimal"
```

---

### Task 2: Per-Request Webhooks

**Files:**
- Modify: `internal/models/job.go:164-172`
- Modify: `internal/redis/client.go` (add HSet for webhook fields in SetJobCreated or new method)
- Modify: `cmd/orchestrator/main.go:404-515`
- Modify: `cmd/completion-worker/worker.py:159-221`
- Test: `internal/redis/client_test.go`

- [ ] **Step 1: Add WebhookURL and WebhookSecret to CreateJobRequest**

In `internal/models/job.go` lines 164-172, add fields:
```go
type CreateJobRequest struct {
    DocumentBase64 string   `json:"document_base64" binding:"required_without=DocumentURL"`
    DocumentURL    string   `json:"document_url" binding:"required_without=DocumentBase64"`
    Filename       string   `json:"filename,omitempty"`
    Features       []string `json:"features,omitempty"`
    WebhookURL     string   `json:"webhook_url,omitempty"`
    WebhookSecret  string   `json:"webhook_secret,omitempty"`
}
```

Run: `go build ./internal/models/`
Expected: SUCCESS

- [ ] **Step 2: Add SetJobWebhook method to Redis client**

In `internal/redis/client.go` after line 413 (after SetJobCreated), add:
```go
// SetJobWebhook stores optional per-job webhook URL and secret in the meta hash.
// Redis key: {namespace}:job:{jobID}:meta (hash with fields "webhook_url" and "webhook_secret")
// TTL: jobTTL (typically 24 hours).
// Returns error if Redis operation fails.
func (c *RedisClient) SetJobWebhook(ctx context.Context, jobID, webhookURL, webhookSecret string) error {
    key := c.key("job", jobID, "meta")
    err := c.client.HSet(ctx, key, map[string]interface{}{
        "webhook_url":    webhookURL,
        "webhook_secret": webhookSecret,
    }).Err()
    if err != nil {
        return fmt.Errorf("failed to set job webhook: %w", err)
    }
    return nil
}
```

Run: `go build ./internal/redis/`
Expected: SUCCESS

- [ ] **Step 3: Call SetJobWebhook in createJobHandler**

In `cmd/orchestrator/main.go` after line 469 (after storing features), add:
```go
// Store webhook config if provided
if req.WebhookURL != "" {
    if err := redis.SetJobWebhook(ctx, jobID, req.WebhookURL, req.WebhookSecret); err != nil {
        logger.Warn().Err(err).Str("job_id", jobID).Msg("Failed to store webhook config")
    } else {
        logger.Info().Str("job_id", jobID).Msg("Webhook config stored")
    }
}
```

Run: `go build ./cmd/orchestrator/`
Expected: SUCCESS

- [ ] **Step 4: Modify completion-worker send_webhook to read from Redis and support HMAC**

In `cmd/completion-worker/worker.py` lines 159-221, refactor `send_webhook` to:
1. Accept optional `webhook_url` and `webhook_secret` parameters
2. If `job_id` is provided, try to read `webhook_url` and `webhook_secret` from Redis `:meta` hash
3. If no per-job webhook, fall back to global `WEBHOOK_URL`
4. If `webhook_secret` is present, compute HMAC-SHA256 and add `X-Webhook-Signature` header

```python
def send_webhook(
    self, job_id: str, status: str, error: Optional[str] = None,
    webhook_url: Optional[str] = None, webhook_secret: Optional[str] = None
) -> bool:
    # Try per-job webhook from Redis first
    if job_id:
        job_webhook_url = self.redis_client.hget(
            f"orchestrator:job:{job_id}:meta", "webhook_url"
        )
        if job_webhook_url:
            webhook_url = job_webhook_url.decode() if isinstance(job_webhook_url, bytes) else job_webhook_url
            job_webhook_secret = self.redis_client.hget(
                f"orchestrator:job:{job_id}:meta", "webhook_secret"
            )
            if job_webhook_secret:
                webhook_secret = job_webhook_secret.decode() if isinstance(job_webhook_secret, bytes) else job_webhook_secret

    webhook_url = webhook_url or WEBHOOK_URL
    if not webhook_url:
        return False

    try:
        payload = {
            "job_id": job_id,
            "status": status,
            "completed_at": datetime.utcnow().isoformat() + "Z",
            "download_url": f"{API_BASE_URL}/v1/documents/{job_id}/download",
        }
        if error:
            payload["error"] = error

        headers = {"Content-Type": "application/json"}
        if webhook_secret:
            import hmac, hashlib
            signature = hmac.new(
                webhook_secret.encode(),
                json.dumps(payload).encode(),
                hashlib.sha256
            ).hexdigest()
            headers["X-Webhook-Signature"] = f"sha256={signature}"
            headers["X-Webhook-Timestamp"] = str(int(time.time()))

        response = requests.post(
            webhook_url,
            json=payload,
            timeout=10,
            headers=headers,
        )
        response.raise_for_status()
        logger.info(f"Webhook sent successfully for job {job_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to send webhook: {e}")
        return False
```

Run: `pytest cmd/completion-worker/tests/test_finalize_job.py -v`
Expected: Existing tests still pass (mocked redis_client.hget)

- [ ] **Step 5: Add Redis test for SetJobWebhook**

In `internal/redis/client_test.go` after TestRedisClient_SetAndGetJobFeatures, add:
```go
func TestRedisClient_SetAndGetJobWebhook(t *testing.T) {
    mr, client := setupTestRedis(t)
    defer mr.Close()
    defer client.Close()

    ctx := context.Background()
    jobID := "test-job-webhook"

    err := client.SetJobWebhook(ctx, jobID, "http://example.com/webhook", "secret123")
    require.NoError(t, err)

    // Verify via HGet
    url, err := client.client.HGet(ctx, "orchestrator:job:test-job-webhook:meta", "webhook_url").Result()
    require.NoError(t, err)
    assert.Equal(t, "http://example.com/webhook", url)

    secret, err := client.client.HGet(ctx, "orchestrator:job:test-job-webhook:meta", "webhook_secret").Result()
    require.NoError(t, err)
    assert.Equal(t, "secret123", secret)
}
```

Run: `go test ./internal/redis/... -run TestRedisClient_SetAndGetJobWebhook -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add internal/models/job.go internal/redis/client.go internal/redis/client_test.go cmd/orchestrator/main.go cmd/completion-worker/worker.py cmd/completion-worker/tests/test_finalize_job.py
git commit -m "feat: add per-request webhook support with HMAC signature"
```

---

### Task 3: OpenAPI Schema with gin-swagger

**Files:**
- Modify: `go.mod`
- Modify: `cmd/orchestrator/main.go` (add swagger annotations)
- Create: `cmd/orchestrator/docs/` (generated)
- Test: Manual verification at `GET /swagger/index.html`

- [ ] **Step 1: Add swaggo dependencies**

```bash
cd /home/hp/Proyectos/ia-text-ochestrator
go get github.com/swaggo/gin-swagger@v1.6.0
go get github.com/swaggo/files@v1.0.1
go get github.com/swaggo/swag/cmd/swag@v1.16.2
```

Run: `go mod tidy && go build ./cmd/orchestrator/`
Expected: SUCCESS

- [ ] **Step 2: Add Swagger annotations to handlers**

In `cmd/orchestrator/main.go`, add doc comments above each handler function:

```go
// createJobHandler
// @Summary Process a document
// @Description Submit a document for async processing. Returns job_id for polling.
// @Accept json
// @Produce json
// @Param request body models.CreateJobRequest true "Document processing request"
// @Success 202 {object} models.CreateJobResponse
// @Failure 400 {object} models.ErrorResponse
// @Failure 500 {object} models.ErrorResponse
// @Router /v1/documents/process [post]
func createJobHandler(c *gin.Context) {

// getJobHandler
// @Summary Get job status and results
// @Description Retrieve current status and results of a processing job.
// @Produce json
// @Param id path string true "Job ID"
// @Success 200 {object} models.GetJobResponse
// @Failure 404 {object} models.ErrorResponse
// @Router /v1/documents/{id} [get]
func getJobHandler(c *gin.Context) {

// downloadHandler
// @Summary Download job results
// @Description Download the full JSON results for a completed job.
// @Produce json
// @Param id path string true "Job ID"
// @Param compression query string false "Compression type (gzip)"
// @Success 200 {object} models.JobResults
// @Failure 404 {object} models.ErrorResponse
// @Router /v1/documents/{id}/download [get]
func downloadHandler(c *gin.Context) {

// deleteJobHandler
// @Summary Delete a job
// @Description Remove a completed or failed job and its associated data.
// @Param id path string true "Job ID"
// @Success 204 "No Content"
// @Failure 404 {object} models.ErrorResponse
// @Router /v1/documents/{id} [delete]
func deleteJobHandler(c *gin.Context) {

// uploadHandler
// @Summary Upload a document
// @Description Upload a document via multipart/form-data for async processing.
// @Accept multipart/form-data
// @Produce json
// @Param file formance file true "Document file"
// @Param filename formData string false "Filename"
// @Param notify_webhook formData string false "Webhook URL for completion notification"
// @Success 202 {object} models.CreateJobResponse
// @Failure 400 {object} models.ErrorResponse
// @Router /v1/documents/upload [post]
func uploadHandler(c *gin.Context) {
```

- [ ] **Step 3: Add swagger imports and setup in main.go**

After imports (around line 38), add:
```go
_ "ia-text-orchestrator/docs/swagger"
"github.com/swaggo/gin-swagger"
"github.com/swaggo/files"
```

And in `setupRouter()` after line 273, add:
```go
r.GET("/swagger/*any", ginSwagger.WrapHandler(swaggerFiles.Handler))
```

- [ ] **Step 4: Generate swagger docs**

```bash
swag init -g cmd/orchestrator/main.go -o docs/swagger --parseDependency --parseInternal
```

Expected: `docs/swagger/swagger.json` created

- [ ] **Step 5: Verify swagger UI**

Run: `go build -o bin/orchestrator cmd/orchestrator/main.go && ./bin/orchestrator &`
Then visit: `http://localhost:8080/swagger/index.html`

- [ ] **Step 6: Commit**

```bash
git add go.mod go.sum cmd/orchestrator/main.go docs/swagger/
git commit -m "feat: add OpenAPI/Swagger documentation"
```

---

## Phase 2: Features

### Task 4: SSE Streaming

**Files:**
- Create: `cmd/orchestrator/handlers/stream.go`
- Modify: `cmd/orchestrator/main.go:250-276` (register route)
- Modify: `internal/events/event_types.go` (add new event type)
- Test: Functional test (manual or integration)

- [ ] **Step 1: Create SSE handler file**

Create `cmd/orchestrator/handlers/stream.go`:
```go
package handlers

import (
    "context"
    "encoding/json"
    "fmt"
    "net/http"
    "time"

    "github.com/gin-gonic/gin"
    "github.com/redis/go-redis/v9"
    "ia-text-orchestrator/internal/events"
    "ia-text-orchestrator/internal/models"
    "ia-text-orchestrator/internal/redis"
)

const (
    sseChannelBuffer = 100
    sseHeartbeat     = 30 * time.Second
    sseMaxDuration   = 10 * time.Minute
)

func StreamJobHandler(c *gin.Context) {
    jobID := c.Param("id")

    // Validate job exists
    ctx, cancel := context.WithTimeout(c.Request.Context(), 5*time.Second)
    defer cancel()

    status, err := redis.GetJobStatus(ctx, jobID)
    if err != nil || status == "" {
        c.JSON(http.StatusNotFound, models.ErrorResponse{
            Error:  "not_found",
            Detail: "job not found",
        })
        return
    }

    // Set SSE headers
    c.Header("Content-Type", "text/event-stream")
    c.Header("Cache-Control", "no-cache")
    c.Header("Connection", "keep-alive")
    c.Header("X-Accel-Buffering", "no")

    // If job already completed/failed, send final event immediately
    if status == models.StatusCompleted || status == models.StatusFailed {
        eventType := "job_completed"
        if status == models.StatusFailed {
            eventType = "job_failed"
        }
        eventData, _ := json.Marshal(map[string]interface{}{
            "job_id":    jobID,
            "status":    string(status),
            "timestamp": time.Now().Format(time.RFC3339),
        })
        c.SSEvent(eventType, string(eventData))
        c.Writer.Flush()
        return
    }

    // Subscribe to job-specific Redis pub/sub channel
    pubsub := eventBus.Subscribe(ctx, fmt.Sprintf("job:%s:events", jobID))
    defer pubsub.Close()

    // Create done channel for graceful shutdown
    done := make(chan struct{})

    // Heartbeat goroutine
    go func() {
        tick := time.NewTicker(sseHeartbeat)
        defer tick.Stop()
        for {
            select {
            case <-tick.C:
                c.Writer.WriteString(": heartbeat\n\n")
                c.Writer.Flush()
            case <-done:
                return
            }
        }
    }()

    // Context with max duration
    ctx, cancel = context.WithTimeout(ctx, sseMaxDuration)
    defer cancel()
    defer close(done)

    // Stream events
    ch := pubsub.Channel()
    for {
        select {
        case msg := <-ch:
            var jobEvent events.JobEvent
            if err := json.Unmarshal([]byte(msg.Payload), &jobEvent); err != nil {
                continue
            }

            eventType := string(jobEvent.EventType)
            eventData, _ := json.Marshal(map[string]interface{}{
                "job_id":    jobEvent.JobID,
                "status":    jobEvent.Status,
                "progress":  jobEvent.Progress,
                "timestamp": jobEvent.Timestamp.Format(time.RFC3339),
                "error":     jobEvent.Error,
            })

            c.SSEvent(eventType, string(eventData))
            c.Writer.Flush()

            // Close if job completed or failed
            if jobEvent.EventType == events.EventJobCompleted || jobEvent.EventType == events.EventJobFailed {
                return
            }

        case <-ctx.Done():
            return
        }
    }
}
```

Note: `eventBus` must be accessible here. Pass it as a dependency or access via package-level variable.

Run: `go build ./cmd/orchestrator/`
Expected: SUCCESS

- [ ] **Step 2: Register SSE route in setupRouter**

In `cmd/orchestrator/main.go` line 266-273, add:
```go
v1.GET("/jobs/:id/stream", StreamJobHandler)  // Note: path is /jobs not /documents per spec
```

Note: The spec says `GET /v1/jobs/:id/stream` but this doesn't match the `/v1/documents/` group. Add it outside the group or adjust the path.

- [ ] **Step 3: Commit**

```bash
git add cmd/orchestrator/handlers/stream.go cmd/orchestrator/main.go internal/events/event_types.go
git commit -m "feat: add SSE streaming endpoint for job progress"
```

---

### Task 5: Batch Processing

**Files:**
- Create: `cmd/orchestrator/handlers/batch.go`
- Create: `cmd/orchestrator/handlers/batch_models.go`
- Modify: `cmd/orchestrator/main.go:250-276`
- Modify: `cmd/completion-worker/worker.py` (batch webhook logic)
- Test: Integration test

- [ ] **Step 1: Create batch request/response models**

Create `cmd/orchestrator/handlers/batch_models.go`:
```go
package handlers

import "time"

type BatchDocument struct {
    Text     string                 `json:"text"`
    Filename string                 `json:"filename,omitempty"`
    Metadata map[string]interface{} `json:"metadata,omitempty"`
}

type BatchRequest struct {
    Documents      []BatchDocument `json:"documents" binding:"required,min=1"`
    MaxConcurrency int            `json:"max_concurrency,omitempty"`
    WebhookURL     string         `json:"webhook_url,omitempty"`
    WebhookSecret  string         `json:"webhook_secret,omitempty"`
}

type BatchJobRef struct {
    ID       string `json:"id"`
    Filename string `json:"filename,omitempty"`
    Status   string `json:"status"`
}

type BatchResponse struct {
    BatchID   string        `json:"batch_id"`
    Total     int           `json:"total"`
    Jobs      []BatchJobRef `json:"jobs"`
    StatusURL string        `json:"status_url"`
    CreatedAt time.Time     `json:"created_at"`
}

type BatchStatusResponse struct {
    BatchID     string        `json:"batch_id"`
    Status      string        `json:"status"`
    Total       int           `json:"total"`
    Completed   int           `json:"completed"`
    Failed      int           `json:"failed"`
    Pending     int           `json:"pending"`
    Jobs        []BatchJobRef `json:"jobs"`
    CreatedAt   time.Time     `json:"created_at"`
    CompletedAt *time.Time    `json:"completed_at,omitempty"`
}
```

Run: `go build ./cmd/orchestrator/handlers/`
Expected: SUCCESS

- [ ] **Step 2: Create batch handler**

Create `cmd/orchestrator/handlers/batch.go`:
```go
package handlers

import (
    "context"
    "fmt"
    "net/http"
    "sync"
    "time"

    "github.com/gin-gonic/gin"
    "github.com/google/uuid"
    "ia-text-orchestrator/internal/models"
    "ia-text-orchestrator/internal/redis"
)

func CreateBatchHandler(c *gin.Context) {
    var req BatchRequest
    if err := c.ShouldBindJSON(&req); err != nil {
        c.JSON(http.StatusBadRequest, models.ErrorResponse{
            Error:  "invalid_request",
            Detail: err.Error(),
        })
        return
    }

    if len(req.Documents) > 100 {
        c.JSON(http.StatusBadRequest, models.ErrorResponse{
            Error:  "invalid_request",
            Detail: "maximum 100 documents per batch",
        })
        return
    }

    maxConcurrency := req.MaxConcurrency
    if maxConcurrency <= 0 {
        maxConcurrency = 10
    }
    if maxConcurrency > 50 {
        maxConcurrency = 50
    }

    batchID := uuid.New().String()
    now := time.Now()

    // Store batch metadata in Redis
    batchMetaKey := redis.Key("batch", batchID, "meta")
    ctx := c.Request.Context()

    err := redis.GetClient().HSet(ctx, batchMetaKey, map[string]interface{}{
        "total":          len(req.Documents),
        "created_at":     now.Unix(),
        "webhook_url":    req.WebhookURL,
        "webhook_secret": req.WebhookSecret,
    }).Err()
    if err != nil {
        // Handle error
    }
    redis.GetClient().Expire(ctx, batchMetaKey, 24*time.Hour)

    // Semaphore for concurrency control
    semaphore := make(chan struct{}, maxConcurrency)
    var wg sync.WaitGroup
    jobs := make([]BatchJobRef, len(req.Documents))

    for i, doc := range req.Documents {
        wg.Add(1)
        go func(i int, doc BatchDocument) {
            defer wg.Done()
            semaphore <- struct{}{}        // Acquire
            defer func() { <-semaphore }() // Release

            jobID := uuid.New().String()

            // Create job in Redis
            jobCtx, cancel := context.WithTimeout(ctx, 30*time.Second)
            defer cancel()

            redis.SetJobStatus(jobCtx, jobID, models.StatusPending)
            redis.SetJobCreated(jobCtx, jobID)

            // Store batch_id in job meta
            redis.GetClient().HSet(jobCtx, redis.Key("job", jobID, "meta"), "batch_id", batchID)

            if req.WebhookURL != "" {
                redis.SetJobWebhook(jobCtx, jobID, req.WebhookURL, req.WebhookSecret)
            }

            // Publish job message
            jobMsg := &models.JobMessage{
                JobID:    jobID,
                Filename: doc.Filename,
            }
            if doc.Metadata != nil {
                // Store metadata
                redis.SetJobMetadata(jobCtx, jobID, doc.Metadata)
            }

            // NOTE: For text-based documents, we need to handle the text field
            // This depends on how the extraction worker expects input
            // For now, we use a placeholder - actual implementation may need
            // to send the text as base64 or via a different mechanism

            mqBroker.PublishJobMessage(jobCtx, jobMsg)

            jobs[i] = BatchJobRef{
                ID:       jobID,
                Filename: doc.Filename,
                Status:   "pending",
            }
        }(i, doc)
    }

    wg.Wait()

    c.JSON(http.StatusAccepted, BatchResponse{
        BatchID:   batchID,
        Total:     len(req.Documents),
        Jobs:      jobs,
        StatusURL: fmt.Sprintf("/v1/batches/%s/status", batchID),
        CreatedAt: now,
    })
}

func GetBatchStatusHandler(c *gin.Context) {
    batchID := c.Param("id")
    ctx := c.Request.Context()

    meta, err := redis.GetClient().HGetAll(ctx, redis.Key("batch", batchID, "meta")).Result()
    if err != nil || len(meta) == 0 {
        c.JSON(http.StatusNotFound, models.ErrorResponse{
            Error:  "not_found",
            Detail: "batch not found",
        })
        return
    }

    // Get job statuses
    jobs, _ := redis.GetClient().SMembers(ctx, redis.Key("batch", batchID, "jobs")).Result()

    var completed, failed, pending int
    jobRefs := make([]BatchJobRef, 0, len(jobs))

    for _, jobID := range jobs {
        status, _ := redis.GetJobStatus(ctx, jobID)
        switch status {
        case models.StatusCompleted:
            completed++
        case models.StatusFailed:
            failed++
        default:
            pending++
        }
        jobRefs = append(jobRefs, BatchJobRef{
            ID:     jobID,
            Status: string(status),
        })
    }

    total := len(jobs)
    var batchStatus string
    if pending == 0 {
        if failed == total {
            batchStatus = "failed"
        } else if failed > 0 {
            batchStatus = "partial"
        } else {
            batchStatus = "completed"
        }
    } else {
        batchStatus = "running"
    }

    createdAt := time.Unix(0, 0)
    if ts, ok := meta["created_at"]; ok {
        if t, err := strconv.ParseInt(ts, 10, 64); err == nil {
            createdAt = time.Unix(t, 0)
        }
    }

    c.JSON(http.StatusOK, BatchStatusResponse{
        BatchID:   batchID,
        Status:    batchStatus,
        Total:     total,
        Completed: completed,
        Failed:    failed,
        Pending:   pending,
        Jobs:      jobRefs,
        CreatedAt: createdAt,
    })
}
```

Run: `go build ./cmd/orchestrator/handlers/`
Expected: May need fixes (redis.Key, mqBroker access patterns)

- [ ] **Step 3: Register batch routes**

In `cmd/orchestrator/main.go`:
```go
v1.POST("/documents/batch", CreateBatchHandler)
v1.GET("/batches/:id/status", GetBatchStatusHandler)
```

Note: Move SSE route outside `/v1/documents/` group or adjust path.

- [ ] **Step 4: Add batch webhook logic to completion-worker**

In `cmd/completion-worker/worker.py` in `finalize_job` or a new method, add batch completion check:

```python
def _check_and_notify_batch(self, job_id: str, status: str):
    """Check if job is part of a batch and notify when batch completes."""
    batch_id = self.redis_client.hget(
        f"orchestrator:job:{job_id}:meta", "batch_id"
    )
    if not batch_id:
        return

    batch_id = batch_id.decode() if isinstance(batch_id, bytes) else batch_id
    batch_done_key = f"orchestrator:batch:{batch_id}:done_count"

    done = self.redis_client.incr(batch_done_key)
    if done == 1:  # First completion, set TTL
        self.redis_client.expire(batch_done_key, 24 * 60 * 60)  # 24h

    total = int(self.redis_client.hget(f"orchestrator:batch:{batch_id}:meta", "total"))

    if done >= total:
        # All jobs done - send batch webhook
        webhook_url = self.redis_client.hget(
            f"orchestrator:batch:{batch_id}:meta", "webhook_url"
        )
        webhook_secret = self.redis_client.hget(
            f"orchestrator:batch:{batch_id}:meta", "webhook_secret"
        )
        if webhook_url:
            webhook_url = webhook_url.decode() if isinstance(webhook_url, bytes) else webhook_url
            webhook_secret = webhook_secret.decode() if isinstance(webhook_secret, bytes) else webhook_secret
            self._send_batch_webhook(batch_id, status, webhook_url, webhook_secret)
```

And add `_send_batch_webhook` method:
```python
def _send_batch_webhook(self, batch_id: str, final_status: str, webhook_url: str, webhook_secret: Optional[str] = None):
    """Send batch completion webhook."""
    try:
        jobs = self.redis_client.smembers(f"orchestrator:batch:{batch_id}:jobs")
        job_statuses = []
        completed = failed = 0

        for job_id in jobs:
            job_id = job_id.decode() if isinstance(job_id, bytes) else job_id
            status, _ = self.redis_client.get(f"orchestrator:job:{job_id}:status")
            status = status.decode() if isinstance(status, bytes) else status
            job_statuses.append({"id": job_id, "status": status})
            if status == "completed":
                completed += 1
            else:
                failed += 1

        if failed == len(jobs):
            batch_status = "failed"
        elif failed > 0:
            batch_status = "partial"
        else:
            batch_status = "completed"

        payload = {
            "batch_id": batch_id,
            "status": batch_status,
            "total": len(jobs),
            "completed": completed,
            "failed": failed,
            "jobs": job_statuses,
        }

        headers = {"Content-Type": "application/json"}
        if webhook_secret:
            import hmac, hashlib
            signature = hmac.new(
                webhook_secret.encode(),
                json.dumps(payload).encode(),
                hashlib.sha256
            ).hexdigest()
            headers["X-Webhook-Signature"] = f"sha256={signature}"

        response = requests.post(webhook_url, json=payload, timeout=10, headers=headers)
        response.raise_for_status()
        logger.info(f"Batch webhook sent for {batch_id}")
    except Exception as e:
        logger.error(f"Failed to send batch webhook: {e}")
```

Call `_check_and_notify_batch` at the end of `finalize_job` after successful completion.

- [ ] **Step 5: Commit**

```bash
git add cmd/orchestrator/handlers/batch.go cmd/orchestrator/handlers/batch_models.go cmd/orchestrator/main.go cmd/completion-worker/worker.py
git commit -m "feat: add batch processing with grouped jobs and batch webhook"
```

---

## Phase 3: Optimization

### Task 6: Embeddings Compression

**Files:**
- Modify: `cmd/orchestrator/main.go:1170-1237` (downloadHandler)
- Test: Manual verification

- [ ] **Step 1: Add compression logic to downloadHandler**

In `cmd/orchestrator/main.go`, modify `downloadHandler` around line 1170:

```go
import (
    // existing imports
    "bytes"
    "compress/gzip"
    "encoding/base64"
    "encoding/json"
)

func downloadHandler(c *gin.Context) {
    jobID := c.Param("id")
    compression := c.Query("compression")  // Add this line

    // ... existing validation code through line 1229 ...

    // Check if compression requested
    if compression == "gzip" {
        // Compress embeddings in each chunk
        for i := range results.Chunks {
            chunk := &results.Chunks[i]
            if len(chunk.Embeddings) > 0 {
                // Serialize float32 to bytes
                buf := new(bytes.Buffer)
                for _, f := range chunk.Embeddings {
                    bits := math.Float32bits(f)
                    buf.WriteByte(byte(bits))
                    buf.WriteByte(byte(bits >> 8))
                    buf.WriteByte(byte(bits >> 16))
                    buf.WriteByte(byte(bits >> 24))
                }

                // Compress with gzip
                var compressed bytes.Buffer
                w := gzip.NewWriter(&compressed)
                w.Write(buf.Bytes())
                w.Close()

                // Base64 encode
                chunk.Embeddings = nil  // Clear original
                // Store compressed as string in a new field or reuse Embeddings field
                // For JSON compatibility, store as base64 string
                compressedStr := base64.StdEncoding.EncodeToString(compressed.Bytes())
                // Use a wrapper or modify Chunk model to support this
                // For now, we'll add a custom field to the response
                type ChunkWithCompression struct {
                    models.Chunk
                    EmbeddingCompressed string `json:"embedding_compressed,omitempty"`
                }
            }
        }

        // For gzip response, marshal manually with custom encoding
        c.Header("Content-Encoding", "gzip")
        c.Header("Content-Disposition", fmt.Sprintf("attachment; filename=results_%s.json.gz", jobID))
        c.Header("Content-Type", "application/json")

        // Custom marshal with compressed embeddings
        type DownloadResponse struct {
            JobID       string `json:"job_id"`
            Compression string `json:"compression"`
            Chunks      []struct {
                ChunkID         string  `json:"chunk_id"`
                Text            string  `json:"text"`
                StartOffset     int     `json:"start_offset"`
                EndOffset       int     `json:"end_offset"`
                EmbeddingCompressed string `json:"embedding_compressed,omitempty"`
            } `json:"chunks"`
        }

        resp := DownloadResponse{
            JobID:       jobID,
            Compression: "gzip",
            Chunks:      make([]struct{ ChunkID, Text, EmbeddingCompressed string; StartOffset, EndOffset int }, len(results.Chunks)),
        }

        for i, chunk := range results.Chunks {
            resp.Chunks[i].ChunkID = chunk.ChunkID
            resp.Chunks[i].Text = chunk.Text
            resp.Chunks[i].StartOffset = chunk.StartOffset
            resp.Chunks[i].EndOffset = chunk.EndOffset

            if len(chunk.Embeddings) > 0 {
                buf := new(bytes.Buffer)
                for _, f := range chunk.Embeddings {
                    bits := math.Float32bits(f)
                    buf.WriteByte(byte(bits))
                    buf.WriteByte(byte(bits >> 8))
                    buf.WriteByte(byte(bits >> 16))
                    buf.WriteByte(byte(bits >> 24))
                }
                var compressed bytes.Buffer
                w := gzip.NewWriter(&compressed)
                w.Write(buf.Bytes())
                w.Close()
                resp.Chunks[i].EmbeddingCompressed = base64.StdEncoding.EncodeToString(compressed.Bytes())
            }
        }

        c.JSON(http.StatusOK, resp)
        return
    }

    // ... existing code for non-compressed response ...
}
```

Note: This approach requires adding `math` to imports and is simplified. A cleaner implementation might create a custom JSON marshaler or modify the Chunk model.

Run: `go build ./cmd/orchestrator/`
Expected: SUCCESS (may have unused import warnings)

- [ ] **Step 2: Commit**

```bash
git add cmd/orchestrator/main.go
git commit -m "feat: add gzip compression for embeddings download"
```

---

## Summary

| Task | Description | Files Modified |
|------|-------------|-----------------|
| 1 | Entity Offsets | `internal/models/job.go`, `cmd/completion-worker/worker.py` |
| 2 | Per-Request Webhooks | `internal/models/job.go`, `internal/redis/client.go`, `cmd/orchestrator/main.go`, `cmd/completion-worker/worker.py` |
| 3 | OpenAPI/Swagger | `go.mod`, `cmd/orchestrator/main.go`, `docs/swagger/` |
| 4 | SSE Streaming | `cmd/orchestrator/handlers/stream.go`, `cmd/orchestrator/main.go` |
| 5 | Batch Processing | `cmd/orchestrator/handlers/batch.go`, `cmd/orchestrator/handlers/batch_models.go`, `cmd/orchestrator/main.go`, `cmd/completion-worker/worker.py` |
| 6 | Embeddings Compression | `cmd/orchestrator/main.go` |
