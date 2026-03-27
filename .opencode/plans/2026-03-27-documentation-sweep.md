# Documentation Sweep — Todo el Proyecto

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add comprehensive technical documentation (docstrings/comments) to all public APIs and model types across the entire project, following native syntax conventions (Go comments, Python Google-style docstrings). Generate architecture diagrams for each major module.

**Architecture:** Systematic sweep across Go (`internal/`, `pkg/`, `cmd/orchestrator`) and Python (`cmd/*/worker.py`) codebase, documenting undocumented functions, types, and classes. Each doc block includes context (purpose/why), contract (inputs/outputs/errors), and consistency with project style.

**Tech Stack:** Go (comments + pkg doc), Python (Google-style docstrings), Mermaid.js for architecture diagrams, no external tools required.

---

## Task 1: Go Models (`internal/models/job.go`)

**Files:**
- Modify: `internal/models/job.go:1-131`

**Context:** The Job model is the central entity in the pipeline. It transitions through lifecycle states (pending → extracting → processing → completed/failed). All 12 types/structs need clear contracts explaining:
- When each state is entered/exited
- What data each struct carries through the pipeline
- How constants vs. derived state work

**Specification:**

- [ ] **Add doc-comment for `Job` struct** (line 7)
  - Explain it's the primary tracking entity from submission to completion
  - Note the Status field is immutable once set (except pending → any)
  - Mention that Results is populated only on completion

- [ ] **Add doc-comment for `JobStatus` type** (line 19)
  - Explain it's an enumeration of pipeline states
  - Document the valid transition paths (pending → * → completed/failed)

- [ ] **Add doc-comments for all 8 JobStatus constants** (lines 22-30)
  - StatusPending: job accepted, awaiting extraction worker
  - StatusExtracting: extraction worker in progress
  - StatusProcessing: deprecated? (if unused, note it)
  - StatusEmbedding: embeddings worker processing
  - StatusEntities: entities worker processing
  - StatusInferences: inference worker (optional feature)
  - StatusCompleted: all workers done, results assembled
  - StatusFailed: unrecoverable error occurred, no further processing

- [ ] **Document remaining types:** `SourceClassificationResult`, `MicroInference`, `ChunkInferences`, `JobResults`, `Chunk`, `DocumentMetadata`, `Entity`, `JobMessage`, `UploadRequest`, `CreateJobRequest`, `CreateJobResponse`, `GetJobResponse`, `ErrorResponse`
  - For each, explain its role in the pipeline (where generated, consumed)
  - Document all JSON field tags, why they exist
  - Note required vs. optional fields

- [ ] **Verify doc-comments are exportable** (run `go doc ./internal/models | grep -E "^(type|const|func)"`)

**Expected output:** All 12 exported types + 8 constants have doc-comments following Go convention (comment precedes symbol, starts with symbol name).

---

## Task 2: Go CircuitBreaker (`internal/middleware/circuitbreaker.go`)

**Files:**
- Modify: `internal/middleware/circuitbreaker.go:1-238`

**Context:** CircuitBreaker has 0 doc-comments currently. It implements a resilience pattern with three states: Closed (requests pass), Open (requests blocked), HalfOpen (testing probes). This is critical infrastructure for preventing cascade failures.

**Specification:**

- [ ] **Add doc-comment for `CircuitState` type** (line 10)
  - Explain it's an iota-based enum for the three automaton states
  - Note that state transitions are deterministic based on error rates

- [ ] **Document all 3 CircuitState constants** (lines 12-15)
  - StateClosed: Normal operation, requests pass
  - StateOpen: Too many failures detected, block all requests
  - StateHalfOpen: In recovery, allow limited test requests

- [ ] **Document error variables** (lines 31-33)
  - ErrCircuitOpen: When circuit is open
  - ErrCircuitTooMany: When too many concurrent probes in half-open

- [ ] **Document `Counts` struct** (lines 36-42)
  - Explain each field: Requests total, Successes, Failures, Timeouts, ContextCancelled, ConcurrencyInFlight
  - Note these are windowed by interval

- [ ] **Document `Settings` struct** (lines 45-51)
  - Name: Identifier for logging/metrics
  - MaxRequests: Limit for half-open state probes
  - Interval: Time window for failure counting
  - Timeout: Duration to wait in open state before attempting half-open
  - ReadyToTrip: Predicate to decide when closed → open
  - IsSuccessful: Predicate to classify errors as success/failure

- [ ] **Document `CircuitBreaker` struct** (line 54)
  - Explain it's not thread-safe on its own (uses mu for synchronization)
  - Note that settings are immutable after NewCircuitBreaker

- [ ] **Document `NewCircuitBreaker` function** (line 67)
  - Explain defaults applied if settings omit values
  - Note starting state is always StateClosed

- [ ] **Document `Execute` method** (line 100)
  - Args: ctx (can be cancelled), fn (the operation to protect)
  - Returns: nil on success, ErrCircuitOpen if open, ErrCircuitTooMany if too many probes, or fn's error
  - Explain that it blocks via mu during state checks

- [ ] **Document other methods:** `beforeRequest`, `afterRequest`, `evaluateState`, `setState`, `State()`, `Counts()`
  - Note which are private vs. exported
  - For exported ones, explain their contract

**Expected output:** All exported types, error vars, and functions documented. Private methods (optional) at least have inline comments explaining state transitions.

---

## Task 3: Go Retry Policy (`internal/middleware/retry.go`)

**Files:**
- Modify: `internal/middleware/retry.go:1-148`

**Context:** 0 doc-comments. Retry is a companion to CircuitBreaker, implementing exponential backoff. Two variants: WithRetry (context-unaware) and WithRetryContext (cancellable).

**Specification:**

- [ ] **Document `RetryPolicy` struct** (line 10)
  - Explain what exponential backoff means
  - Note that if RetryableErrors is nil, all errors are retryable (permissive default)
  - Document each field's effect on retry behavior

- [ ] **Document `DefaultRetryPolicy()` function** (line 18)
  - Explain the conservative defaults: 3 retries, 1s initial delay, 2x backoff

- [ ] **Document `WithRetry` function** (line 27)
  - Signature: Takes fn and policy, returns error
  - Behavior: Calls fn up to MaxRetries+1 times (initial call + retries)
  - Does NOT respect context cancellation (use WithRetryContext for that)
  - Returns: nil on first success, or wrapped final error like "after 3 retries: <error>"
  - Note: isRetryable check filters which errors trigger retry

- [ ] **Document `WithRetryContext` function** (line 65)
  - Like WithRetry but respects ctx.Done() during inter-attempt sleeps
  - Returns ctx.Err() immediately if context cancelled
  - Otherwise same error format as WithRetry

- [ ] **Document `isRetryable` helper** (line 115)
  - Explain nil RetryableErrors means all errors retry
  - Document how errors.Is is used for matching

- [ ] **Document `RetryableError` type and methods** (lines 129-148)
  - Explain it's a wrapper to mark errors as explicitly retryable
  - Document Unwrap (for errors.As compatibility), Error() method
  - Explain when to use NewRetryableError vs. passing to policy.RetryableErrors

**Expected output:** All exported functions and types documented. The two retry functions clearly contrasted (context handling).

---

## Task 4: Go Pipeline Orchestrator (`internal/pipeline/orchestrator.go`)

**Files:**
- Modify: `internal/pipeline/orchestrator.go:1-189`

**Context:** Pipeline coordinates the fan-out to three independent workers. Currently only 3 doc-comments inline. This is the core orchestration logic.

**Specification:**

- [ ] **Document `Pipeline` struct** (line 16)
  - Explain it's stateless between calls (all state in Redis and queues)
  - Note that it coordinates three concurrent workers: embeddings, entities, metadata
  - Mention optional fourth worker: inference (if features requested)

- [ ] **Document `NewPipeline` function** (line 23)
  - Args: broker (RabbitMQ), redis (state store), cfg (configuration)
  - Returns: *Pipeline ready for ProcessInParallel calls
  - Note: Does NOT validate that queues exist (declared by workers or broker init)

- [ ] **Document `PipelineResult` struct** (line 32)
  - Explain each field: EmbeddingsResult (vector), EntitiesResult (list), MetadataResult (map), Errors (collected non-fatal errors), Duration (wall-clock time)
  - Note that partial failures do NOT abort the pipeline

- [ ] **Document `ProcessInParallel` method** (line 40)
  - Args: ctx, jobID, text (extracted document text)
  - Behavior: Fan-out to 3 goroutines (embeddings, entities, metadata)
  - Returns: PipelineResult with all available data + any errors encountered
  - Errors returned: Only fatal (ctx cancelled before dispatch); non-fatal errors accumulate in result
  - Note: This method does NOT wait for workers to complete (that's WaitForCompletion's job)

- [ ] **Document private methods:**
  - `processEmbeddings`: Stores text in Redis, publishes to embeddings queue, logs progress
  - `processEntities`: Publishes to entities queue (doesn't need text from Redis already set)
  - `processMetadata`: Same as entities

- [ ] **Document `WaitForCompletion` method** (line 157)
  - Args: ctx, jobID, timeout (fallback deadline)
  - Behavior: Polls Redis every 500ms checking job status
  - Returns: JobResults on completion, error on failure or timeout
  - Errors: ctx.Err() if cancelled, "timeout waiting for job completion" if deadline passed, unwrapped error message from Redis if job failed
  - Explain how deadline is resolved: ctx.Deadline() if set, otherwise time.Now().Add(timeout)

**Expected output:** All 4 public types/methods fully documented with examples of typical call flow.

---

## Task 5: Go EventBus (`internal/events/event_bus.go`)

**Files:**
- Modify: `internal/events/event_bus.go:1-95`

**Context:** Event bus uses Redis pub/sub to broadcast job status changes. 0 doc-comments. Subscribing to events allows external clients to monitor progress.

**Specification:**

- [ ] **Document `EventBus` struct** (line 14)
  - Explain it's a pub/sub wrapper around Redis
  - Note that it publishes structured JobEvent messages to channels

- [ ] **Document `NewEventBus` function** (line 19)

- [ ] **Document `Publish` method** (line 26)
  - Internal publish to a channel
  - Args: ctx, channel (Redis pub/sub key), event (JobEvent pointer)
  - Returns: error if marshal or Redis publish fails

- [ ] **Document all `PublishJob*` methods** (lines 52-91)
  - `PublishJobCreated`: Fire when job is created, status=pending
  - `PublishJobProgress`: Fire during processing with progress % and current step
  - `PublishJobCompleted`: Fire when job succeeds with metadata
  - `PublishJobFailed`: Fire when job fails with error message
  - `PublishJobEvent`: Generic publish to job-specific channel (for internal use)

**Expected output:** All methods documented with typical usage patterns (e.g., when completion-worker calls PublishJobCompleted).

---

## Task 6: Go ContentCache (`internal/cache/content_cache.go`)

**Files:**
- Modify: `internal/cache/content_cache.go:1-97`

**Context:** Simple caching layer using Redis. No public use currently, but critical if used. 0 doc-comments.

**Specification:**

- [ ] **Document `ContentCache` struct** (line 15)
  - Purpose: Cache expensive computations (e.g., embeddings) keyed by content hash
  - Note: Uses SHA-256 of input as cache key, auto-expires after defaultTTL

- [ ] **Document `NewContentCache` function** (line 21)

- [ ] **Document `GetOrCompute` method** (line 28)
  - Typical cache pattern: if cached, return; else compute, store, return
  - Args: ctx, key (string input), compute (thunk function returning interface{})
  - Returns: The computed/cached value, or error from compute()
  - Note: Compute function is called OUTSIDE the critical section (no lock held)

- [ ] **Document `Get` method** (line 55)
  - Retrieve cached value without computing
  - Returns: nil, redis.Nil if not found; nil, error if unmarshal fails

- [ ] **Document private helper `computeHash`** (inline comment only)

**Expected output:** All methods clear on caching semantics.

---

## Task 7: Go Redis Client (`internal/redis/client.go`)

**Files:**
- Modify: `internal/redis/client.go:1-482`

**Context:** 30+ exported functions, currently only `key()` documented. This is the critical state store for the entire pipeline. High risk of undocumented behavior.

**Specification:**

- [ ] **Document `RedisClient` struct** (line 18)
  - Purpose: Encapsulates all state persistence for jobs, embeddings, entities, metadata
  - Namespace field: Allows isolating multiple deployments in same Redis instance
  - jobTTL field: Auto-expire jobs (default 24h) to prevent unbounded growth

- [ ] **Document `New` function** (line 25)
  - Args: cfg (must have RedisURL + JobTTL + JobNamespace)
  - Behavior: Parses URL, applies 5s dial + 3s read/write + 4s pool timeouts
  - Returns: Connected client or error if URL invalid or connection fails
  - Pings Redis before returning (fail-fast)

- [ ] **Document `GetClient()` method** (line 73)
  - Returns raw redis.Client for advanced use
  - Note: Breaks encapsulation; prefer typed methods if possible

- [ ] **Document Job Status methods** (lines 84-106)
  - `SetJobStatus`: Stores status in Redis hash, applies TTL
  - `GetJobStatus`: Retrieves status, returns redis.Nil if job expired

- [ ] **Document Data Storage methods** (lines 108-240)
  - `SetJobText` / `GetJobText`: Raw extracted text
  - `SetJobResults` / `GetJobResults`: Full assembled JobResults
  - `SetJobEmbeddings` / `GetJobEmbeddings`: Vector embeddings (float32 slice)
  - `SetJobEntities` / `GetJobEntities`: Named entities list
  - `SetJobMetadata` / `GetJobMetadata`: Document metadata map

- [ ] **Document Job Lifecycle methods** (lines 246-337)
  - `UpdateJobStep`: Updates step status (e.g., "embeddings" → "completed")
  - `GetJobSteps`: Returns map of all step statuses
  - `SetJobCreated` / `GetJobCreated`: Timestamps
  - `SetJobCompleted`: Marks job done
  - `SetJobError`: Stores error message
  - `SetJobFeatures`: Requested features (e.g., ["inferences"])
  - `GetJobFeatures`: Retrieves features list
  - `DeleteJob`: Hard-delete all keys for a job (for testing or cleanup)

- [ ] **Document Cleanup and Health methods** (lines 353-480)
  - `DeleteJob`: Removes all job data from Redis
  - `HealthCheck`: Pings Redis
  - `ExpireStuckJobs`: Finds jobs with status != terminal + past timeout, marks them failed
  - `Close`: Closes Redis connection

- [ ] **Document key schema** (inline comments above key() or in main doc)
  - Pattern: `{namespace}:job:{jobID}:{field}`
  - Examples: "orchestrator:job:abc123:status", "orchestrator:job:abc123:embeddings"

**Expected output:** All 30+ methods documented. Key schema clearly explained so workers can predict Redis keys.

---

## Task 8: Go Orchestrator Handlers (`cmd/orchestrator/main.go`)

**Files:**
- Modify: `cmd/orchestrator/main.go:1-897`

**Context:** Main REST API surface. Only 2/10 functions documented (validateJobID, validateDocumentInput). Critical handlers like createJobHandler, getJobHandler lack contracts.

**Specification:**

- [ ] **Document `main()` function** (line 54)
  - High-level flow: parse config, init Redis/RabbitMQ, setup routes, start server
  - Note: Blocks until interrupt signal

- [ ] **Document `setupRouter()` function** (line 228)
  - Registers all routes (POST/GET/DELETE /documents, GET /health, etc.)
  - Returns configured *gin.Engine

- [ ] **Document middleware functions:**
  - `ginLogger()` (line 256): Logs all HTTP requests using zeroLog
  - `metricsMiddleware()` (line 288): Instruments request latency and counts

- [ ] **Document Handler Functions** (the critical ones):
  - `healthHandler` (line 310): GET /health — Returns service health status
  - `createJobHandler` (line 329): POST /documents — Creates new job
    - Input: CreateJobRequest (document_base64 OR document_url, features list, notify_webhook)
    - Validation: SSRF check on URL, document size limits, base64 decode check
    - Output: CreateJobResponse (job_id, status=pending, status_url)
    - Errors: 400 Bad Request, 422 Unprocessable Entity (SSRF violation), 500 Internal
  - `getJobHandler` (line 434): GET /documents/{jobID} — Poll job status
    - Input: jobID (UUID format checked by validateJobID middleware)
    - Output: GetJobResponse (includes status, results if completed, error if failed, steps map)
    - Errors: 404 Not Found, 500 Internal
  - `deleteJobHandler` (line 492): DELETE /documents/{jobID} — Hard-delete job
    - Idempotent: Returns 204 whether job existed or not
  - `uploadHandler` (line 672): POST /documents/upload (multipart/form-data)
    - Accepts single file, returns document_base64 ready for createJobHandler
  - `downloadHandler` (line 897): GET /documents/{jobID}/download — Download results as JSON file

- [ ] **Document Helper Functions:**
  - `generateJobID()` (line 533): Returns UUID v4 hex string
  - `validateJobID()` (line 539): Check jobID is valid UUID format (already documented)
  - `validateDocumentInput()` (line 560): Check SSRF, size limits (already documented)

**Expected output:** All handlers have clear contracts on inputs, outputs, error codes, and side effects (Redis writes, queue publishes).

---

## Task 9: Python Extraction Worker (`cmd/extraction-worker/worker.py`)

**Files:**
- Modify: `cmd/extraction-worker/worker.py:1-709`

**Context:** 709 lines, 5 docstrings, 0 on main classes. This is the first worker in the pipeline; it orchestrates Docling extraction, chunking, and source classification.

**Specification:**

- [ ] **Document module docstring** (top of file)
  - Purpose: Extract text and metadata from documents using Docling API
  - Inputs: Document file (PDF, DOCX, etc.) from Redis or URL
  - Outputs: Extracted text, chunks, metadata to Redis; publishes to downstream queues

- [ ] **Document standalone functions** (missing Google-style docstrings):
  - `compute_file_hash(file_bytes)`: SHA-256 digest of raw bytes
  - `extract_pdf_metadata(file_path, filename)`: Parse EXIF/XMP via exiftool
  - `chunk_text(text, chunk_size, overlap)`: Token-based chunking (tiktoken or approx)
  - `analyze_text(text)`: Lightweight analytics (char count, language, readability)

- [ ] **Document `SourceClassifier` class** (line 241)
  - Purpose: Classify document source (notariado, catastro, bancario, etc.)
  - Method `classify()`: Takes text, returns SourceClassificationResult

- [ ] **Document `ExtractionWorker` class** (line 304)
  - Purpose: Main RabbitMQ consumer for extraction queue
  - Methods:
    - `__init__()`: Connect to Redis, RabbitMQ, init Docling client
    - `process_message()`: Main handler for incoming extraction jobs
    - `start()`, `stop()`, `signal_handler()`: Lifecycle management

- [ ] **Document critical workflow** (inline comments):
  - Download document (file path or URL) → write to /tmp/
  - Extract metadata (exiftool)
  - Extract text (Docling API)
  - Chunk text (tiktoken-based)
  - Classify source type
  - Store results in Redis
  - Publish to downstream queues (embeddings, entities, metadata, inferences)

**Expected output:** All functions and classes have Google-style docstrings with Args, Returns, Raises sections.

---

## Task 10: Python Completion Worker (`cmd/completion-worker/worker.py`)

**Files:**
- Modify: `cmd/completion-worker/worker.py:1-410`

**Context:** 410 lines, 5 docstrings, CompletionWorker class undocumented. This is the final aggregator in the pipeline.

**Specification:**

- [ ] **Document module docstring** (top of file)
  - Purpose: Monitor job progress via Redis pub/sub, aggregate results, mark completed

- [ ] **Document `CompletionWorker` class** (line 42)
  - Purpose: Subscribe to job progress events, collect all worker results when complete
  - Fields: redis_client, event_bus, required_steps (which workers must finish)
  - Methods:
    - `__init__()`: Setup Redis, event bus, define default required steps
    - `save_results_to_file()`: Write final JobResults to /results/{job_id}.json
    - `send_webhook()`: POST completed job notification to webhook URL
    - `check_job_completion()`: Poll all required steps, return True if all done
    - `process_event()`: Handle incoming job progress events

- [ ] **Document key methods:**
  - `save_results_to_file(job_id, results)`: Stores JobResults as JSON; returns bool success
  - `send_webhook(job_id, status, error)`: Sends webhook if WEBHOOK_URL configured; returns bool success
  - `check_job_completion()`: Returns True once all default_required_steps are in "completed" status

- [ ] **Document the main loop:**
  - Subscribe to "job:events" Redis channel
  - For each JobProgress event, poll Redis to collect current state
  - Once all steps complete, finalize: save to file, send webhook, mark status=completed in Redis

**Expected output:** CompletionWorker class and all public methods fully documented.

---

## Task 11: Python Metadata Worker (`cmd/metadata-worker/worker.py`)

**Files:**
- Modify: `cmd/metadata-worker/worker.py:1-201`

**Context:** 201 lines, 4 docstrings, MetadataWorker class undocumented.

**Specification:**

- [ ] **Document module docstring**
  - Purpose: Extract document and text-level metadata without ML models

- [ ] **Document `MetadataWorker` class** (line 42)
  - Purpose: Lightweight metadata extraction (char count, language heuristic, hash, etc.)
  - Methods: `__init__()`, `extract_metadata()`, `detect_language()`, `process_message()`

- [ ] **Document key methods:**
  - `extract_metadata(text, document_url)`: Compute char/word/line counts, content hash, language guess, readability score
  - `detect_language(text)`: Heuristic language detection (check for common words, script, etc.)
  - `process_message(job_message)`: Handle incoming job from RabbitMQ, extract metadata, store to Redis

**Expected output:** All methods documented.

---

## Task 12: Python Inference Worker (`cmd/inference-worker/worker.py`)

**Files:**
- Modify: `cmd/inference-worker/worker.py:1-431`

**Context:** 431 lines, some methods documented, but `__init__()`, lifecycle, and class-level doc missing.

**Specification:**

- [ ] **Add missing docstrings:**
  - `InferenceWorker` class (line 40): Purpose, responsibilities
  - `__init__()` method (line 41): State initialization, model discovery
  - `process_message()`: Main handler for inference jobs from RabbitMQ

- [ ] **Ensure full contracts:**
  - `_discover_model(llm_url)`: Already documented
  - `_extract_outermost_array(text)`: Already documented
  - `extract_inferences(chunk_text, entities, source_type, max_inferences)`: Already documented

- [ ] **Document main lifecycle:**
  - Startup: Try to discover model from vLLM /v1/models API
  - Fallback: If discovery fails, use LLM_MODEL env var (if set)
  - Process: For each incoming job, extract inferences by querying LLM
  - Shutdown: Cleanup signal handlers

**Expected output:** InferenceWorker fully documented with clear state transitions (discovery → fallback → processing).

---

## Task 13: Architecture Documentation (4 files)

**Files:**
- Create: `README_ARCHITECTURE.md` (root)
- Create: `internal/README_ARCHITECTURE.md`
- Create: `cmd/README_ARCHITECTURE.md`
- Create: `pkg/README_ARCHITECTURE.md`

**Specification for each:**

Each README should include:
1. **Module Purpose** (2-3 sentences)
2. **Key Components** (list of major types/classes)
3. **Responsibilities** (what this module owns)
4. **Dependencies** (what it depends on)
5. **Mermaid Diagram** (internal interactions + external services)
6. **Key Data Structures** (major types, with brief description)
7. **Common Patterns** (e.g., error handling, lifecycle)

**Root README (`README_ARCHITECTURE.md`):**
- Overview of the entire system
- Full architecture diagram (all services, queues, databases)
- Data flow from upload to completion
- Key design decisions (event-driven, microservices, air-gapped offline)

**Internal Package (`internal/README_ARCHITECTURE.md`):**
- Role: Core Go infrastructure (broker, cache, config, health, middleware, models, pipeline, redis)
- Diagram: Interactions between modules
- Detail: Redis key schema, RabbitMQ queues, circuit breaker states, retry backoff

**Command (Workers) (`cmd/README_ARCHITECTURE.md`):**
- Role: Distributed workers (extraction, embeddings, entities, inference, metadata, completion)
- Diagram: Worker → queues → Redis → event bus
- Detail: Each worker's input/output contract, error handling, metrics

**Packages (`pkg/README_ARCHITECTURE.md`):**
- Role: Shared code (logging, metrics, events)
- Diagram: How they're used by other modules
- Detail: Logger configuration, Prometheus metrics hierarchy

**Expected output:** 4 markdown files with Mermaid diagrams that can be viewed in GitHub/docs sites.

---

## Success Criteria

✅ All public functions/types/classes have doc-comments or docstrings
✅ Go comments follow Go convention (symbol first, starts with name)
✅ Python uses Google-style docstrings (Args, Returns, Raises)
✅ Error behavior documented (what errors can be raised, when)
✅ 4 architecture READMEs created with Mermaid diagrams
✅ All tests still pass (`make test && make test-python`)
✅ Code compiles cleanly (Go) and lints cleanly (Python with black/isort)
✅ Git history is clean (one commit per task, descriptive messages)

---
