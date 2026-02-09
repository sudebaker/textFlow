# Copilot Instructions for IA Text Orchestrator

Event-driven microservices architecture for document processing with Go orchestrator and Python ML workers.

## 🔴 CRITICAL: Entities Worker Offline Mode Issue

**CURRENT STATUS:** The entities-worker uses GLiNER for named entity extraction but **FAILS IN PRODUCTION** because the `gliner` library makes unauthorized calls to HuggingFace even with `local_files_only=True`.

### The Problem

```python
# This code in worker.py still tries to download from HuggingFace:
model = GLiNER.from_pretrained(
    model_path,
    local_files_only=True,  # ❌ DOESN'T WORK - GLiNER ignores this
    config=custom_config,
)
```

**Why it fails:**
1. GLiNER internally uses transformers which attempts to download model components
2. Environment variables `TRANSFORMERS_OFFLINE=1` and `HF_HUB_OFFLINE=1` are sometimes ignored
3. The library checks for newer model versions online even when local files exist
4. Missing tokenizer files cause fallback to online download

### Impact

- **Development:** Works fine (internet access available)
- **Production:** FAILS (air-gapped environment, no internet)
- **Current extraction:** 35 entities instead of expected 150-200 (77% regression)

### Files Involved

```
cmd/entities-worker/
├── worker.py                    # Main worker (lines 69-141: load_model)
├── gliner_extractor.py         # Standalone extractor
├── download_gliner_models.py   # Model downloader (incomplete)
├── offline_diagnosis.py        # Diagnostic script
├── test_offline_mode.py        # Offline test
└── requirements.txt            # gliner, torch, transformers
```

### Required Model Files for Offline Operation

GLiNER requires these files in `/models/gliner-small-v2.1/`:

```
Essential:
- config.json                    # Model configuration
- gliner_config.json            # GLiNER-specific config
- pytorch_model.bin             # Model weights
- tokenizer_config.json         # Tokenizer configuration
- spm.model                     # SentencePiece tokenizer
- special_tokens_map.json       # Special tokens
- vocab.txt or tokenizer.json  # Vocabulary

Optional (improves performance):
- model.safetensors             # Alternative to pytorch_model.bin
```

### Current Workaround (Temporary)

The worker has fallback heuristics for offline operation but they're limited:

```python
def _extract_dates(self, text: str) -> List[Dict]:
    # Regex-based fallback (lines 143-166)
    
def _extract_money(self, text: str) -> List[Dict]:
    # Regex-based fallback (lines 168-189)
    
def _extract_persons(self, text: str) -> List[Dict]:
    # Regex-based fallback (lines 235-264)
```

**Accuracy:** ~60-70% vs 90-95% with proper GLiNER model

### Solutions in Progress

**Option 1: Fix GLiNER Loading (RECOMMENDED)**
- Download ALL required model files upfront
- Use `snapshot_download` from huggingface_hub during build
- Mount complete model to `/models/` in container
- Ensure tokenizer files are present

**Option 2: Replace GLiNER**
- Use spaCy with downloaded models (more reliable offline)
- Use Flair NER (better offline support)
- Custom BERT-based NER with explicit local loading

**Option 3: Hybrid Approach**
- Primary: GLiNER (if properly loaded)
- Fallback: Heuristic extraction (current implementation)
- Monitoring: Alert if using fallback mode

### How to Test Offline Mode

```bash
# 1. Block internet access
docker run --network=none entities-worker

# 2. Set offline environment variables
export TRANSFORMERS_OFFLINE=1
export HF_HUB_OFFLINE=1
export HF_DATASETS_OFFLINE=1

# 3. Run diagnostic
python cmd/entities-worker/offline_diagnosis.py

# 4. Run offline test
python cmd/entities-worker/test_offline_ner.py
```

Expected output: Model loads successfully WITHOUT network requests.

### Action Items for Contributors

Before working on entities-worker:

1. **Always test offline mode** - Use `--network=none` Docker flag
2. **Verify model files** - Check all required files exist in `/models/`
3. **Monitor logs** - Look for "Downloading" or "HuggingFace" warnings
4. **Check metrics** - Entities extracted should be 100-150, not 35

## Architecture Overview

```
Document → [Orchestrator:8080] → RabbitMQ → [Extract Worker] → 3 parallel queues
                                                    ├→ [Embeddings Worker]
                                                    ├→ [Entities Worker] ⚠️ OFFLINE ISSUE HERE
                                                    └→ [Metadata Worker]
                                                              ↓
                                                    [Completion Worker] → Redis
                                                              ↓
                                                    Status: completed
```

**Services:**
- **orchestrator** (Go/Gin): REST API on port 8080
- **resource-manager** (Go): Resource monitoring on port 9090
- **embeddings-worker** (Python/FastAPI): BAAI/bge-m3 model for text embeddings
- **entities-worker** (Python/FastAPI): ⚠️ GLiNER model (offline issues)
- **extraction-worker** (Python/FastAPI): Text extraction via Unstructured API
- **metadata-worker** (Python/FastAPI): Document metadata analysis
- **completion-worker** (Python): Job completion aggregator

**Infrastructure:**
- RabbitMQ for job queuing
- Redis for state management and Pub/Sub events
- Unstructured API for document parsing

## Build, Test, and Lint Commands

### Makefile Commands (Preferred)
```bash
make help                         # Show all available commands

# Development
make run-orchestrator             # Run orchestrator on port 8080
make run-resource                 # Run resource manager on port 9090
make run-embeddings-worker        # Run embeddings worker
make run-entities-worker          # Run entities worker ⚠️
make run-workers                  # Run all Python workers
make run-all                      # Run all services locally

# Infrastructure
make infra-up                     # Start RabbitMQ, Redis, Unstructured
make infra-down                   # Stop infrastructure
make docker-up                    # Start all with docker-compose
make docker-down                  # Stop all services
make docker-logs                  # Follow all logs

# Testing
make test                         # Run all Go tests
make test-coverage               # Run tests with coverage HTML
make test-python                 # Run all Python tests

# Quality
make lint                         # Run Go linter (golangci-lint)
make lint-fix                     # Fix linter issues
make format                       # Format Go and Python code

# Build
make build                        # Build all Go binaries
make build-orchestrator           # Build orchestrator binary
make build-resource-manager      # Build resource-manager binary
```

### Running Single Tests

**Go:**
```bash
go test -v ./internal/redis/...              # Single package
go test -v ./internal/redis/client_test.go   # Single file
go test -v -run TestSetJobStatus ./...       # Single test function
go test -v -cover ./...                      # With coverage
```

**Python:**
```bash
pytest cmd/embeddings-worker/tests/ -v                       # Single worker tests
pytest cmd/embeddings-worker/tests/test_api.py -v           # Single test file
pytest cmd/embeddings-worker/tests/test_api.py::test_extract_success -v  # Single test
pytest cmd/*/tests -v --cov=app --cov-report=html           # With coverage HTML
```

**Entities Worker Specific Tests:**
```bash
# Test offline model loading
python cmd/entities-worker/offline_diagnosis.py

# Test entity extraction without network
python cmd/entities-worker/test_offline_ner.py

# Test with network isolation
docker run --network=none entities-worker python test_offline_ner.py
```

### Docker Commands
```bash
docker compose build                          # Build all images
docker compose up -d                          # Start all detached
docker compose logs -f orchestrator           # Follow orchestrator logs
docker compose logs -f entities-worker        # Monitor entities worker ⚠️
docker compose exec -it redis redis-cli       # Access Redis CLI

# Test offline mode
docker run --network=none entities-worker python worker.py  # Should work but doesn't
```

## Key Conventions

### Redis Keys (Namespaced)
All Redis keys use namespace prefix (default: "orchestrator"):
- `orchestrator:job:{id}:status` - Job status
- `orchestrator:job:{id}:text` - Extracted text
- `orchestrator:job:{id}:embeddings` - Embedding vectors
- `orchestrator:job:{id}:entities` - Named entities ⚠️ (often incomplete)
- `orchestrator:job:{id}:metadata` - Document metadata
- `orchestrator:job:{id}:chunks` - Text chunks for processing

### RabbitMQ Queues
- `extract_text` - Document extraction
- `embeddings` - Embedding generation
- `entities` - Named entity extraction ⚠️ (offline issues)
- `metadata` - Metadata analysis

### Event Bus (Redis Pub/Sub)
Used for cross-service communication:
- `job:created` - New job initiated
- `step:completed` - Processing step finished
- `job:completed` - All steps done

### Python Import Organization
Organize in three sections, sorted alphabetically:
```python
# Standard library
import logging
import os
from typing import Dict, Optional

# Third-party packages
import pika
import redis
from fastapi import FastAPI

# Local imports
from app.config.settings import Settings
from app.services.embeddings import EmbeddingService
```

### Naming Conventions
- **Classes**: PascalCase (e.g., `EmbeddingService`, `RedisClient`)
- **Functions/Variables**: snake_case (e.g., `generate_embeddings`, `job_id`)
- **Constants**: UPPER_SNAKE_CASE (e.g., `MAX_RETRIES`, `GLINER_MODEL_PATH`)
- **Private members**: Leading underscore (e.g., `_redis_client`, `_extract_dates`)
- **Go exported**: PascalCase; unexported: camelCase

### Error Handling

**Python (FastAPI):**
```python
try:
    result = await process_document(doc)
except ValueError as e:
    logger.warning(f"Validation error: {e}")
    raise HTTPException(status_code=400, detail=str(e))
except Exception as e:
    logger.error(f"Processing failed: {e}")
    raise HTTPException(status_code=500, detail="Processing error")
```

**Go:**
```go
func processDocument(doc Document) error {
    result, err := redis.GetJobStatus(ctx, jobID)
    if err != nil {
        logger.Error().Err(err).Msgf("Failed to get status for job %s", jobID)
        return fmt.Errorf("job not found: %w", err)
    }
    return nil
}
```

### Structured Logging
**Python:**
```python
logger.info(f"Processing job: {job_id}", extra={"job_id": job_id})
logger.warning(f"Model fallback mode activated", extra={
    "job_id": job_id, 
    "reason": "offline_mode"
})
```

**Go:**
```go
logger.Info().
    Str("job_id", jobID).
    Str("status", status).
    Msg("Job status updated")
```

### Configuration
Use environment variables with validation:

**Python (Pydantic):**
```python
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    redis_url: str = "redis://localhost:6379"
    rabbitmq_url: str
    gliner_model_path: str = "/models/gliner-small-v2.1"
    allow_remote_download: bool = False  # ⚠️ Must be False in production
    
    class Config:
        env_prefix = "APP_"
```

**Go:**
```go
type Config struct {
    RabbitMQURL string `env:"RABBITMQ_URL,required"`
    RedisURL    string `env:"REDIS_URL" default:"redis://localhost:6379"`
    HTTPPort    int    `env:"HTTP_PORT" default:"8080"`
}
```

### Environment Variables for Entities Worker

**Required:**
```bash
REDIS_URL=redis://localhost:6379
RABBITMQ_URL=amqp://localhost:5672/
GLINER_MODEL_PATH=/models/gliner-small-v2.1
```

**Critical for Production:**
```bash
# Force offline mode
TRANSFORMERS_OFFLINE=1
HF_HUB_OFFLINE=1
HF_DATASETS_OFFLINE=1
ALLOW_REMOTE_DOWNLOAD=false

# Entity extraction tuning
ENTITY_TYPES=PER,ORG,LOC,DATE,MONEY
DEDUPLICATION_ENABLED=false              # Currently disabled due to over-aggressive filtering
FUZZY_MATCH_THRESHOLD=0.85               # Too low, causes false positives

# Per-entity thresholds (current regression issue)
ENTITY_THRESHOLD_PER=0.35               # OK
ENTITY_THRESHOLD_ORG=0.50               # OK
ENTITY_THRESHOLD_LOC=0.50               # OK
ENTITY_THRESHOLD_DATE=0.60              # ⚠️ TOO HIGH - causes 40% rejection
ENTITY_THRESHOLD_MONEY=0.65             # ⚠️ TOO HIGH - causes 30% rejection
```

**Recommended fixes:**
```bash
ENTITY_THRESHOLD_DATE=0.45              # Better recall
ENTITY_THRESHOLD_MONEY=0.55             # Better recall
DEDUPLICATION_ENABLED=false             # Disable until improved
```

### Type Hints (Python 3.11+)
Use Pydantic models for request/response:
```python
from typing import List, Dict, Optional
from pydantic import Field, BaseModel

class JobRequest(BaseModel):
    job_id: str = Field(..., description="Unique job identifier")
    document_base64: Optional[str] = None
    entity_types: List[str] = Field(
        default=["PER", "ORG", "LOC", "DATE", "MONEY"],
        description="Entity types to extract"
    )
```

### FastAPI Patterns
```python
@router.post("/process", response_model=JobResponse)
async def process_document(request: JobRequest) -> JobResponse:
    """Process a document and return job ID."""
    # implementation
```

### Gin Framework Patterns
```go
func createJobHandler(c *gin.Context) {
    var req models.CreateJobRequest
    if err := c.ShouldBindJSON(&req); err != nil {
        c.JSON(http.StatusBadRequest, models.ErrorResponse{
            Error: "invalid_request",
            Detail: err.Error(),
        })
        return
    }
    // implementation
}
```

## Environment Variables

**Required for all services:**
```bash
REDIS_URL=redis://localhost:6379
RABBITMQ_URL=amqp://localhost:5672/
UNSTRUCTURED_URL=http://localhost:8000
```

**Optional:**
```bash
APP_LOG_LEVEL=info          # debug, info, warn, error
APP_HTTP_PORT=8080          # Default: 8080 (orchestrator)
```

## Quick Start for Development

```bash
# 1. Start infrastructure
make infra-up

# 2. Verify services are running
curl http://localhost:8080/health
redis-cli ping
docker exec -it rabbitmq rabbitmqctl status

# 3. Run orchestrator (new terminal)
make run-orchestrator

# 4. Run workers (separate terminals)
make run-embeddings-worker
make run-entities-worker     # ⚠️ May fail in offline mode

# 5. Test a document
curl -X POST http://localhost:8080/v1/documents/process \
  -H "Content-Type: application/json" \
  -d '{"document_url": "https://example.com/doc.pdf"}'

# 6. Check status
curl http://localhost:8080/v1/documents/{job_id}

# 7. Verify entities extraction
redis-cli GET "orchestrator:job:{job_id}:entities"
# Expected: 100-150 entities
# Current issue: Only ~35 entities
```

## Known Issues

### 1. Entities Worker Offline Mode (CRITICAL)
- **Status:** 🔴 BLOCKING PRODUCTION
- **Issue:** GLiNER makes unauthorized HuggingFace calls
- **Impact:** 77% reduction in entity extraction (35 vs 150 entities)
- **Workaround:** Heuristic fallback (60-70% accuracy)
- **Fix in progress:** See "CRITICAL: Entities Worker Offline Mode Issue" section above

### 2. Entity Deduplication Over-Aggressive
- **Status:** 🟡 IDENTIFIED
- **Issue:** FUZZY_MATCH_THRESHOLD=0.85 causes false positives
- **Impact:** "María Pérez" and "María Pérez" collapse to one entity
- **Workaround:** Set DEDUPLICATION_ENABLED=false
- **Fix:** Implement exact-match deduplication or increase threshold to 0.95+

### 3. Date/Money Thresholds Too High
- **Status:** 🟡 IDENTIFIED
- **Issue:** ENTITY_THRESHOLD_DATE=0.60, MONEY=0.65
- **Impact:** Rejects 40% of valid dates, 30% of valid money entities
- **Workaround:** Lower to DATE=0.45, MONEY=0.55
- **Fix:** See ANALISIS_ENTIDADES_WORKER.md for detailed analysis

## Performance Considerations

- Load ML models once at startup (not per request)
- Use connection pooling for Redis and RabbitMQ
- Context timeouts on all operations (5-30s depending on operation)
- Prefetch count for consumers (5-10 messages)
- Redis pipelining for batch operations when possible
- **⚠️ GLiNER model loading takes 5-15 seconds** - do NOT reload per request

## Security Considerations

- Validate all inputs (document size limits, URL whitelisting)
- Prevent SSRF attacks (block localhost, cloud metadata endpoints)
- Use environment variables for secrets (no hardcoded credentials)
- Network segmentation (databases not exposed externally)
- Circuit breaker for external service calls
- **⚠️ PRODUCTION MUST DISABLE internet access to workers** (air-gapped)

## Documentation References

For deeper analysis of entities-worker issues, see:
- `ANALISIS_ENTIDADES_WORKER.md` - Comprehensive analysis of extraction issues
- `ARQUITECTURA_NUEVA_ENTIDADES.md` - Architecture considerations
- `cmd/entities-worker/README.md` - Worker-specific documentation
- `data/output/` - Analysis output files and benchmarks

## Getting Help

When reporting issues with entities-worker:

1. **Include logs:** Check for "Downloading", "HuggingFace", or "Offline mode" messages
2. **Verify model files:** Run `ls -la /models/gliner-small-v2.1/`
3. **Check entity counts:** Should be 100-150, not 35
4. **Test offline:** Use `docker run --network=none`
5. **Check environment:** Verify all `*_OFFLINE=1` variables are set
