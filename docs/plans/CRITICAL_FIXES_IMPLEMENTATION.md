# IA Text Orchestrator — Critical & High Issues Implementation Plan

**Date:** 2026-03-16  
**Status:** In Progress  
**Author:** Senior Systems Engineer  
**Target:** Production-ready fixes in 3 sequential batches

---

## Executive Summary

The codebase has solid microservices architecture but accumulates critical bugs that prevent production deployment:

1. **API endpoint missing** — `POST /v1/documents/process` not registered, breaking documented interface
2. **Regex patterns broken** — double backslashes in entities worker extract nothing, causing "35 entities vs 150-200" regression
3. **Non-unique job IDs** — concurrent requests generate collisions, silent data corruption
4. **Security issues** — path traversal, SSRF exceptions, missing validation
5. **Goroutine leaks** — metrics collector, health checks, reconnection logic

**Fix strategy:** Three independent batches. Each batch tested before proceeding. No breaking changes to API contracts.

---

## Batch 1 — Production Blockers

**Objective:** System must start, receive documents, process without data loss or CVEs.  
**Est. Time:** 2 hours  
**Verification:** `make build && make test && make test-python`

### 1.1 Fix Go version in `go.mod` + migrate to `rabbitmq/amqp091-go`

**File:** `go.mod`  
**Changes:**
- Line 3: `go 1.25.5` → `go 1.22.5`
- Line 12: Replace `github.com/streadway/amqp v1.1.0` with `github.com/rabbitmq/amqp091-go v1.10.0`

**Post-change:** `go mod tidy`, then update imports in `internal/broker/rabbitmq.go`:
```
github.com/streadway/amqp → github.com/rabbitmq/amqp091-go
```

**Why:** Streadway/amqp is unmaintained; RabbitMQ team now owns the fork. Go 1.25 doesn't exist; using 1.22.5 (stable LTS).

---

### 1.2 Remove `go.sum` from `.gitignore`

**File:** `.gitignore`  
**Change:** Delete line containing `go.sum`

**Post-change:** `git add go.sum`

**Why:** `go.sum` is cryptographic verification of the module graph. Ignoring it breaks reproducibility and enables supply-chain attacks.

---

### 1.3 Remove DEBUG statements from hot path

**File:** `cmd/orchestrator/main.go`  
**Lines:** 359, 365  
**Remove:**
```go
fmt.Fprintf(os.Stderr, "DEBUG: Job status: '%s' ...")
fmt.Fprintf(os.Stderr, "DEBUG: Status is completed, fetching results ...")
```

**Why:** Confirmed in AGENTS.md as known issue; still in production code. Every GET request writes stderr, polluting logs.

---

### 1.4 Register `createJobHandler` on router + add `POST /v1/documents/process`

**File:** `cmd/orchestrator/main.go`, `setupRouter()` function (line 179)  
**Change:** Add after line 184:
```go
v1.POST("/documents/process", createJobHandler)
```

**Why:** The handler exists but is unreachable. This endpoint runs SSRF validation + event publishing. Currently only `uploadHandler` (multipart) is available.

---

### 1.5 Replace `generateJobID()` with UUID instead of `UnixNano`

**File:** `cmd/orchestrator/main.go` (line 423)  
**Changes:**
1. Add to `go.mod`: `github.com/google/uuid v1.6.0`
2. Replace function:
```go
import "github.com/google/uuid"

func generateJobID() string {
    return uuid.New().String()
}
```

**Why:** `time.Now().UnixNano()` collides under concurrent load → silent data corruption. UUIDs are cryptographically unique.

---

### 1.6 Protect `Close()` with `sync.Once`

**File:** `internal/broker/rabbitmq.go`  
**Changes:**
1. Add field to struct (line 34):
```go
closeOnce sync.Once
```

2. Replace line 401:
```go
b.closeOnce.Do(func() { close(b.stopChan) })
```

**Why:** Calling `Close()` twice panics with "close of closed channel". `sync.Once` ensures idempotency.

---

### 1.7 Fix `isReconnecting` data race with `atomic.Bool`

**File:** `internal/broker/rabbitmq.go`  
**Changes:**
1. Line 34, replace field:
```go
isReconnecting atomic.Bool  // import "sync/atomic"
```

2. Line 449-454, replace the mutex check:
```go
if !b.isReconnecting.CompareAndSwap(false, true) {
    return  // Already reconnecting
}
defer b.isReconnecting.Store(false)

// Remove all manual b.reconnectMutex lock/unlock calls
```

**Why:** Current code reads `isReconnecting` without lock while another goroutine writes → data race → both attempt reconnect simultaneously.

---

### 1.8 Add context-aware shutdown to `StartMetricsCollector()`

**File:** `pkg/metrics/metrics.go` (line 223)  
**Change:**
```go
func StartMetricsCollector(ctx context.Context) {
    go func() {
        ticker := time.NewTicker(10 * time.Second)
        defer ticker.Stop()
        for {
            select {
            case <-ctx.Done():
                return
            case <-ticker.C:
                collectRuntimeMetrics()
            }
        }
    }()
}
```

**File:** `cmd/orchestrator/main.go` (line 60)  
**Change:**
```go
metrics.StartMetricsCollector(ctx)
```

**Why:** Goroutine leak on shutdown. Metrics collector runs forever with no stop mechanism.

---

### 1.9 Initialize logger with config log level after loading config

**File:** `cmd/orchestrator/main.go` (line 46 + after line 52)  
**Change:**
```go
// Line 46: keep temporary init
logging.Init("info")
logger = logging.GetLogger()

// After line 52 (after config.Load):
logging.Init(cfg.LogLevel)
logger = logging.GetLogger()
```

**Why:** `LOG_LEVEL` env var currently ignored. Logger initialized before config loads.

---

### 1.10 Fix regex patterns in `entities-worker` — double backslash bug

**File:** `cmd/entities-worker/worker.py`  
**Methods affected:** `_extract_dates`, `_extract_money`, `_extract_orgs`, `_extract_locs`, `_extract_persons`  
**Global change:** In all raw strings, replace double backslash with single:
- `\\d` → `\d`
- `\\$` → `\$`
- `\\s` → `\s`
- `\\b` → `\b`
- etc.

**Example (line 179):**
```python
# Before:
r"\\d{1,2}/\\d{1,2}/\\d{2,4}"

# After:
r"\d{1,2}/\d{1,2}/\d{2,4}"
```

**Why:** This is the root cause of "~35 entities vs 150-200" in AGENTS.md. Raw strings already prevent backslash escaping; doubling them creates literal `\d` (two chars) which never matches.

---

### 1.11 Fix `metadata-worker` Redis key — add `orchestrator:` prefix

**File:** `cmd/metadata-worker/worker.py` (line 164)  
**Change:**
```python
# Before:
self.redis_client.hset(f"job:{job_id}:status", mapping={"metadata": "error"})

# After:
self.redis_client.hset(f"orchestrator:job:{job_id}:status", mapping={"metadata": "error"})
```

**Why:** All other code uses `orchestrator:job:{id}:status`. This error update is written to a key that's never read → metadata errors silently lost.

---

### 1.12 Add file extension whitelist + path escape validation in `uploadHandler`

**File:** `cmd/orchestrator/main.go` (line 545)  
**Changes:**
1. After line 565 (after `filename := filepath.Base(...)`):
```go
// Add import "regexp" at top
var allowedExtensions = map[string]bool{
    ".pdf": true, ".docx": true, ".doc": true,
    ".txt": true, ".html": true, ".xlsx": true, ".pptx": true,
}

ext := strings.ToLower(filepath.Ext(filename))
if !allowedExtensions[ext] {
    c.JSON(http.StatusBadRequest, models.ErrorResponse{
        Error:  "invalid_file_type",
        Detail: fmt.Sprintf("file type %s is not allowed", ext),
    })
    return
}
```

2. After line 567 (after `filePath := filepath.Join(...)`):
```go
absUploadPath, _ := filepath.Abs(cfg.UploadPath)
absFilePath, _ := filepath.Abs(filePath)
if !strings.HasPrefix(absFilePath, absUploadPath+string(filepath.Separator)) {
    c.JSON(http.StatusBadRequest, models.ErrorResponse{Error: "invalid_filename"})
    return
}
```

**Why:** No MIME validation or extension check. Any `.sh` or executable can be uploaded. No path traversal guard.

---

### 1.13 Sanitize `jobID` parameter in all handlers

**File:** `cmd/orchestrator/main.go`  
**Add after line 10 (imports):**
```go
var validJobIDRegex = regexp.MustCompile(`^[a-zA-Z0-9_-]{1,64}$`)
```

**Apply to handlers:**
1. `getJobHandler` (line 343): Add after `jobID := c.Param("id")`:
```go
if !validJobIDRegex.MatchString(jobID) {
    c.JSON(http.StatusBadRequest, models.ErrorResponse{Error: "invalid_job_id"})
    return
}
```

2. `deleteJobHandler` (line 391): Same check
3. `downloadHandler` (line 654): Same check + path escape check:
```go
absResultsPath, _ := filepath.Abs(cfg.ResultsPath)
absFilePath, _ := filepath.Abs(resultsPath)
if !strings.HasPrefix(absFilePath, absResultsPath+string(filepath.Separator)) {
    c.JSON(http.StatusBadRequest, models.ErrorResponse{Error: "invalid_path"})
    return
}
```

**Why:** `jobID` from URL params used directly in filesystem paths without validation → path traversal via `../` sequences.

---

### 1.14 Fix health check HTTP status mapping

**File:** `cmd/orchestrator/main.go` (line 242)  
**Change:**
```go
httpStatus := http.StatusOK  // Default to 200
if healthStatus.Status == "unhealthy" {
    httpStatus = http.StatusServiceUnavailable  // 503
}
// "degraded" still returns 200 (system is partially operational)
```

**Why:** Current code maps `"degraded"` → 503, removing instance from load balancer even though it's still serving. Only truly "unhealthy" should be 503.

---

### 1.15 Delete `docker-compose.old` from repository

**File:** `docker-compose.old`  
**Action:** `git rm deploy/docker/docker-compose.old`

**Why:** Contains hardcoded plaintext credentials and fully exposed ports. Security liability.

---

## Batch 2 — High Priority / Reliability & Security

**Objective:** Fix remaining high-severity issues. Context-aware cancellation, proper SSRF, thread safety.  
**Est. Time:** 3 hours  
**Dependencies:** Batch 1 must compile successfully  
**Verification:** `make build && make test && make test-python && make lint`

### 2.1 Remove Docker IP SSRF exception + implement `AllowLocalURLs` config check

**File:** `cmd/orchestrator/main.go`  
**Function signature change (line 428):**
```go
func validateDocumentInput(req *models.CreateJobRequest, cfg *config.Config) error {
```

**Changes:**
1. Remove lines 498-509 and 529-534 (Docker IP exception)
2. Update `blockedHosts` (line 478):
```go
blockedHosts := []string{
    "169.254.169.254",          // AWS/Azure/GCP IMDS
    "metadata.google.internal", // GCP metadata
    "metadata.azure.internal",  // Azure metadata
    "100.100.100.200",          // Alibaba Cloud metadata
    "metadata",
}
```

3. Around line 473, add config check:
```go
// Block localhost and loopback addresses (unless explicitly allowed)
if !cfg.AllowLocalURLs {
    if hostname == "localhost" || hostname == "127.0.0.1" || hostname == "::1" {
        return fmt.Errorf("localhost URLs are not allowed")
    }
    // ... similar for IsPrivate() and IsLoopback() checks
}
```

4. Update caller in `createJobHandler` (line 271):
```go
if err := validateDocumentInput(&req, cfg); err != nil {
```

**Why:** Docker range `172.16.0.0/12` allows reaching internal services (Redis, RabbitMQ). `AllowLocalURLs` currently never read.

---

### 2.2 Use context-aware DNS resolution instead of blocking `net.LookupIP`

**File:** `cmd/orchestrator/main.go` (line 519)  
**Change:**
```go
// Before:
ips, err := net.LookupIP(hostname)

// After:
resolver := net.DefaultResolver
ipaddrs, err := resolver.LookupIPAddr(ctx, hostname)
if err != nil {
    return fmt.Errorf("failed to resolve hostname: %w", err)
}
ips := make([]net.IP, len(ipaddrs))
for i, addr := range ipaddrs {
    ips[i] = addr.IP
}
```

**Why:** `net.LookupIP` has no timeout → blocks HTTP handler indefinitely. Using request context respects the 30s timeout.

---

### 2.3 Context-aware sleeps in `Publish()` retry loop

**File:** `internal/broker/rabbitmq.go` (line 180)  
**Changes in retry loop (around line 193):**
```go
// Before:
time.Sleep(time.Duration(attempt+1) * time.Second)

// After:
select {
case <-ctx.Done():
    return ctx.Err()
case <-time.After(time.Duration(attempt+1) * time.Second):
}
```

**Why:** Current code sleeps blindly, ignoring caller's context cancellation. HTTP handlers wait unnecessarily after client disconnects.

---

### 2.4 Context-aware sleep in `reconnect()` loop

**File:** `internal/broker/rabbitmq.go` (line 471)  
**Change:**
```go
// Before:
time.Sleep(backoff)

// After:
select {
case <-b.stopChan:
    return
case <-time.After(backoff):
}
```

**Why:** Prevents reconnect loop from being uninterruptible during shutdown.

---

### 2.5 Log errors from `Expire()` calls on all 4 sites

**File:** `internal/redis/client.go`  
**Lines to fix:** 90, 250, 269, 291  
**Pattern:**
```go
// Before:
c.client.Expire(ctx, key, c.jobTTL)

// After:
if err := c.client.Expire(ctx, key, c.jobTTL).Err(); err != nil {
    c.logger.Warn().Err(err).Str("key", key).Msg("failed to set TTL on key")
}
```

**Why:** Silent TTL failures → Redis fills indefinitely. Need visibility into these transient errors.

---

### 2.6 Fix hardcoded queue names in `pipeline/orchestrator.go`

**File:** `internal/pipeline/orchestrator.go`  
**Changes:**
1. Add field to struct: `config *config.Config`
2. Line 114: Replace `"embeddings"` with `p.config.EmbeddingsQueue`
3. Line 129: Replace `"entities"` with `p.config.EntitiesQueue`
4. Line 144: Replace `"metadata"` with `p.config.MetadataQueue`

**Also fix in:** `internal/health/checker.go` (line 128)  
**Pattern:**
```go
// Before:
queues := []string{"embeddings", "entities", "metadata", "extract_text"}

// After:
queues := []string{
    h.config.EmbeddingsQueue,
    h.config.EntitiesQueue,
    h.config.MetadataQueue,
    h.config.ExtractQueue,
}
```

**Why:** Hardcoded names diverge from config if environment variables change.

---

### 2.7 Fix `completion-worker` O(n²) entity deduplication

**File:** `cmd/completion-worker/worker.py` (lines 98-103)  
**Replace entire method:**
```python
def deduplicate_entities(self, entities: List[Dict]) -> List[Dict]:
    """Deduplicate entities by text+label, keeping highest confidence."""
    seen: Dict[str, Dict] = {}
    for entity in entities:
        key = f"{entity.get('text', '')}:{entity.get('label', '')}"
        if key not in seen or entity.get("confidence", 0) > seen[key].get("confidence", 0):
            seen[key] = entity
    return list(seen.values())
```

**Why:** Current uses `list.index()` in loop → O(n²). For thousands of entities, blocks event loop for seconds.

---

### 2.8 Add reconnection recovery to `completion-worker` pubsub

**File:** `cmd/completion-worker/worker.py` (lines 249-256)  
**Replace the main loop:**
```python
def run(self) -> None:
    logger.info("Completion worker started")
    while not self._shutdown_requested:
        try:
            pubsub = self.redis_client.pubsub()
            pubsub.subscribe("job:events")
            for message in pubsub.listen():
                if self._shutdown_requested:
                    break
                if message["type"] == "message":
                    self.handle_event(message)
        except redis.exceptions.ConnectionError as e:
            logger.warning(f"Redis connection lost, reconnecting in 2s: {e}")
            time.sleep(2)
        except Exception as e:
            logger.error(f"Pubsub error: {e}", exc_info=True)
            time.sleep(5)
        finally:
            try:
                pubsub.close()
            except:
                pass
    logger.info("Completion worker stopped")
```

**Why:** Current code crashes on Redis disconnect with no recovery. Pubsub needs infinite retry loop.

---

### 2.9 Fix path traversal in `extraction-worker` document URL

**File:** `cmd/extraction-worker/worker.py` (lines 320-332)  
**Replace:**
```python
from pathlib import Path

UPLOAD_BASE_DIR = Path("/app/data/uploads").resolve()

url_path = document_url.split("/data/uploads/")[-1]
local_path = (UPLOAD_BASE_DIR / url_path).resolve()

# Verify path doesn't escape the upload directory
if not str(local_path).startswith(str(UPLOAD_BASE_DIR)):
    raise ValueError(f"Path traversal attempt blocked: {url_path}")
```

**Why:** `document_url` with `../` sequences escapes the uploads directory.

---

### 2.10 Remove per-request RabbitMQ connection in health check

**File:** `pkg/worker_common/base.py` (lines 291-294)  
**Replace:**
```python
def _check_rabbitmq(self) -> bool:
    """Check if RabbitMQ connection is alive."""
    try:
        return (
            self._connection is not None
            and self._connection.is_open
        )
    except Exception:
        return False
```

**Why:** Current code opens new connection per healthcheck request (every 10s). Exhausts broker connection slots under active liveness probes.

---

### 2.11 Move HuggingFace offline env vars BEFORE imports

**File:** `cmd/embeddings-worker/worker.py` (move lines 29-30 to top, before line 1)  
**New top of file:**
```python
import os

# MUST be set before any HuggingFace library imports
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
os.environ["ALLOW_REMOTE_DOWNLOAD"] = "false"

# Now safe to import:
import sys
sys.path.insert(0, "/app")
```

**Why:** Current order lets `import EmbeddingService` initialize HuggingFace in online mode first.

---

### 2.12 Block online downloads when `ALLOW_REMOTE_DOWNLOAD=false`

**File:** `cmd/embeddings-worker/app/services/embeddings.py` (line 155)  
**Replace:**
```python
# Before:
logger.info(f"Local model not found at {model_path}, downloading from HuggingFace...")
self.model = SentenceTransformer(self.model_name, device=self.device)

# After:
allow_remote = os.getenv("ALLOW_REMOTE_DOWNLOAD", "false").lower() == "true"
if not allow_remote:
    raise RuntimeError(
        f"Local model not found at {model_path}. "
        f"ALLOW_REMOTE_DOWNLOAD={allow_remote}. "
        "Set ALLOW_REMOTE_DOWNLOAD=true or pre-download the model."
    )
logger.info(f"Downloading model from HuggingFace: {self.model_name}")
self.model = SentenceTransformer(self.model_name, device=self.device)
```

**Why:** AGENTS.md requires air-gapped deployments. This code bypasses the config check entirely.

---

### 2.13 Infrastructure: critical fixes in `docker-compose.yml`

**File:** `deploy/docker/docker-compose.yml`  
**Changes:**

1. **Add restart policy** (lines 3-24, 26-48):
```yaml
# After "image: rabbitmq:3.12-management":
restart: unless-stopped

# After "image: redis:7.2-alpine":
restart: unless-stopped
```

2. **Fix embeddings-worker HF_HUB_OFFLINE** (line 161):
```yaml
# Before:
HF_HUB_OFFLINE: "0"

# After:
HF_HUB_OFFLINE: "1"
ALLOW_REMOTE_DOWNLOAD: "false"
```

3. **Mount models read-only** (lines 153, 190):
```yaml
# Before:
- ../../models:/models

# After:
- ../../models:/models:ro
```

4. **Restrict Docling port to localhost** (line 267):
```yaml
# Before:
ports:
  - "8000:5001"

# After:
ports:
  - "127.0.0.1:8000:5001"
```

5. **Fix entities-worker memory limits** (lines 187-188 → use deploy.resources):
```yaml
# Before:
mem_limit: 4g
memswap_limit: 4g

# After (remove above, add):
deploy:
  resources:
    limits:
      memory: 4G
      cpus: "2.0"
    reservations:
      memory: 2G
      cpus: "1.0"
```

**Why:** Infrastructure security hardening + proper restart + config alignment.

---

### 2.14 Correct threshold values in `.env.example`

**File:** `.env.example`  
**Changes:**
```bash
# Line 42: Before:
ALLOW_REMOTE_DOWNLOAD=true

# After:
ALLOW_REMOTE_DOWNLOAD=false

# Lines 47-49: Before:
ENTITY_THRESHOLD_DATE=0.60
ENTITY_THRESHOLD_MONEY=0.65

# After (AGENTS.md recommendations):
ENTITY_THRESHOLD_DATE=0.45
ENTITY_THRESHOLD_MONEY=0.55
```

**Why:** Example values contradict AGENTS.md "known issues" section and cause entity rejection regression.

---

## Batch 3 — Medium Priority / Technical Debt

**Objective:** Code quality, maintainability, testing infrastructure.  
**Est. Time:** 2 hours  
**Dependencies:** Batch 1 & 2 complete  
**Verification:** `make build && make test && make format && make lint`

### 3.1 Don't log raw query string in access logs

**File:** `cmd/orchestrator/main.go` (line 204)  
**Change:**
```go
// Before:
Str("query", query).

// After (scrub sensitive data):
u, _ := url.ParseQuery(query)
var queryKeys []string
for k := range u {
    queryKeys = append(queryKeys, k)
}
sort.Strings(queryKeys)
Strs("query_params", queryKeys).
```

**Why:** Query strings may contain API keys, tokens, passwords. Don't log full values.

---

### 3.2 Fix TOCTOU race in `circuitbreaker.go::beforeRequest()`

**File:** `internal/middleware/circuitbreaker.go` (lines 110-133)  
**Replace:**
```go
// Before: lock released, then re-acquired later
cb.mu.Lock()
state := cb.state
cb.mu.Unlock()
// ... gap where state can change ...
cb.mu.Lock()
cb.requests++

// After: atomic operation
func (cb *CircuitBreaker) beforeRequest() error {
    cb.mu.Lock()
    defer cb.mu.Unlock()  // Hold lock entire function
    
    switch cb.state {
    case StateHalfOpen:
        if cb.requests >= int(cb.settings.MaxRequests) {
            cb.state = StateClosed
        }
        cb.requests++
    // ... rest of logic under lock ...
    }
    return nil
}
```

**Why:** Between lock release and reacquisition, another goroutine can change state → inconsistent behavior.

---

### 3.3 Add timeouts + graceful shutdown to `resource-manager`

**File:** `cmd/resource-manager/main.go`  
**Changes:**
```go
// After srv := &http.Server{:
srv := &http.Server{
    Addr:              addr,
    Handler:           r,
    ReadHeaderTimeout: 5 * time.Second,
    ReadTimeout:       10 * time.Second,
    WriteTimeout:      10 * time.Second,
    IdleTimeout:       60 * time.Second,
}

// Replace srv.Close() with:
shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
defer cancel()
if err := srv.Shutdown(shutdownCtx); err != nil {
    logger.Error().Err(err).Msg("failed to shutdown server gracefully")
    srv.Close()
}
```

**Why:** No timeouts → Slowloris vulnerability. `Close()` kills in-flight requests; `Shutdown()` waits gracefully.

---

### 3.4 Consolidate three `Settings` classes in `entities-worker`

**Files:** `cmd/entities-worker/app/config/settings.py`, `worker.py`, `main.py`  
**Action:**
1. Keep only `cmd/entities-worker/app/config/settings.py` as canonical
2. Remove hand-rolled `Settings` class from `main.py`
3. Replace env reads in `worker.py` with imports from `app/config/settings.py`
4. Set defaults to `ENTITY_THRESHOLD_DATE=0.45`, `ENTITY_THRESHOLD_MONEY=0.55`

**Why:** Three independent config sources cause inconsistency and confusion.

---

### 3.5 Consolidate `parse_rabbitmq_url()` in single location

**Files:** 6 copies exist  
**Action:**
1. Keep canonical implementation in `pkg/worker_common/rabbitmq.py`
2. Delete copies from all 5 workers
3. Each worker imports: `from pkg.worker_common.rabbitmq import parse_rabbitmq_url`

**Why:** DRY violation. Copies have slightly different retry parameters, creating subtle bugs.

---

### 3.6 Pin Python image base versions + add non-root users

**Dockerfiles:** All Python workers + docling-server  
**Pattern changes:**
```dockerfile
# Before:
FROM python:3.11-slim

# After:
FROM python:3.11.12-slim-bookworm

# Add to all:
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Add non-root user where missing (extraction, completion, docling):
RUN adduser --disabled-password --gecos "" app && \
    mkdir -p /app && \
    chown -R app:app /app
USER app
```

**Orchestrator Dockerfile:**
```dockerfile
# Before:
FROM golang:1.25-alpine

# After:
FROM golang:1.22.5-alpine3.21 AS builder
RUN CGO_ENABLED=0 GOOS=linux go build -ldflags="-w -s" -o orchestrator .
```

**Why:** Pin patch versions for reproducibility. Non-root improves security. `PYTHONUNBUFFERED` fixes log buffering.

---

### 3.7 Fix `Makefile` docker-compose references

**File:** `Makefile`  
**Changes:**
```makefile
# Add at top:
COMPOSE_FILE := deploy/docker/docker-compose.yml

# Update docker targets:
docker-up:
	docker compose -f $(COMPOSE_FILE) up -d

docker-down:
	docker compose -f $(COMPOSE_FILE) down

# Fix infra-up (line 158):
infra-up:
	docker compose -f $(COMPOSE_FILE) up -d rabbitmq redis docling
# (was: "rabbitmq redis unstructured" — unstructured doesn't exist)
```

**Why:** Missing `-f` causes docker-compose to not find file from wrong directory.

---

### 3.8 Fix test fixtures: use correct Redis types

**File:** `internal/testutils/fixtures.go`  
**Changes:**
1. Line 96-100: `MustSetJobStatus` should use `HSet` (hash), not `Set` (string):
```go
func (tc *TestRedisClient) MustSetJobStatus(ctx context.Context, jobID, status string) {
    key := "orchestrator:job:" + jobID + ":status"
    err := tc.client.HSet(ctx, key, "status", status).Err()
    // was: tc.client.Set(ctx, key, status, 0).Err()
}
```

2. Line 311-314: `AssertJobStatus` should use `HGet` (hash), not `Get` (string):
```go
func (ah *AssertionHelpers) AssertJobStatus(...) {
    actual, err := redis.HGet(ctx, "orchestrator:job:"+jobID+":status", "status")
    // was: redis.Get(ctx, ...)
}
```

**Why:** Application uses HSet/HGet; fixtures used Get/Set. This caused assertions to always fail or pass vacuously.

---

### 3.9 Delete unused/dead code files

**Files to delete:**
- `cmd/embeddings-worker/Dockerfile.worker` (orphaned, duplicate of `Dockerfile`)
- `cmd/embeddings-worker/worker_refactored.py` (not executed, confuses maintainers)
- `internal/testutils/fixtures.go` unused functions (lines 134-151): `serializeFloat64Slice`, `formatFloat`, `float64ToString`

**Why:** Dead code increases maintenance burden and confusion.

---

### 3.10 Add CI/CD workflow (GitHub Actions)

**New file:** `.github/workflows/ci.yml`
```yaml
name: CI

on: [push, pull_request]

jobs:
  test-go:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-go@v4
        with:
          go-version: "1.22.5"
      - run: go mod download
      - run: go test ./...
      - run: golangci-lint run

  test-python:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-python@v4
        with:
          python-version: "3.11"
      - run: pip install -r requirements.txt pytest
      - run: pytest cmd/*/tests/ -v
```

**Why:** Automated testing catches regressions. No current CI exists.

---

## Verification Checkpoints

### After Batch 1:
```bash
make build              # Must compile
make test               # Go tests pass
make test-python        # Python tests pass
git diff go.sum         # Verify go.sum is tracked
```

### After Batch 2:
```bash
make build && make test && make test-python
make lint               # golangci-lint passes
docker compose -f deploy/docker/docker-compose.yml config  # Validate compose
```

### After Batch 3:
```bash
make build && make test && make lint && make format
make test-coverage      # Coverage report
docker build -f cmd/orchestrator/Dockerfile -t orchestrator:test .  # Test builds
```

---

## Rollback Strategy

Each task is atomic. If a task fails:
1. `git checkout -- <modified files>`
2. Review the error
3. Re-apply with fix
4. Continue to next task

No task depends on uncommitted work; commit after each batch passes tests.

---

## Timeline Estimate

- **Batch 1:** 2 hours (straightforward fixes)
- **Batch 2:** 3 hours (context/threading complexity)
- **Batch 3:** 2 hours (cleanup/refactoring)
- **Total:** ~7 hours

---

## Success Criteria

✅ System starts without panics or errors  
✅ `make build && make test && make test-python` all pass  
✅ `POST /v1/documents/process` accepts requests + publishes events  
✅ `uuid.New()` generates unique job IDs (no collisions under concurrent load)  
✅ Entities worker extracts 150-200 entities, not 35  
✅ Health endpoint returns 200 for healthy/degraded, 503 for unhealthy  
✅ All Docker images build successfully  
✅ `make infra-up && make run-orchestrator` brings up full stack  

---

**Status:** Ready for execution  
**Next Step:** Start Batch 1
