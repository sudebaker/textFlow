# Testing Guide - IA Text Orchestrator

## Running Tests

### Go Tests

#### All Tests
```bash
go test ./... -v -cover
```

#### Specific Package
```bash
# Redis client tests
go test -v ./internal/redis/... -cover

# Broker tests
go test -v ./internal/broker/... -cover

# Handler tests
go test -v ./cmd/orchestrator/... -cover
```

#### With Race Detector
```bash
go test -race ./...
```

#### Coverage Report (HTML)
```bash
go test ./internal/redis/... -coverprofile=coverage.out
go tool cover -html=coverage.out -o coverage.html
```

### Python Tests (Workers)

#### Embeddings Service
```bash
cd embeddings-service
pytest tests/ -v --cov=app --cov-report=html
```

#### GLiNER Service
```bash
cd gliner-service
pytest cmd/gliner-service/tests -v --cov
```

## Test Coverage Status

### Go Modules

| Module | Coverage | Status |
|--------|----------|--------|
| `internal/redis` | 75.3% | ✅ Passing (16 tests) |
| `internal/broker` | TBD | ⏳ Pending |
| `internal/config` | TBD | ⏳ Pending |
| `cmd/orchestrator` | TBD | ⏳ Pending |

**Target:** 70% coverage for Go code

### Python Modules

| Module | Coverage | Status |
|--------|----------|--------|
| Embeddings Service | TBD | ⏳ Pending |
| GLiNER Service | TBD | ⏳ Pending |
| Workers | TBD | ⏳ Pending |

**Target:** 60% coverage for Python code

## Test Structure

### Go Tests

Tests follow the naming convention `*_test.go` and are located alongside the code they test:

```
internal/
├── redis/
│   ├── client.go
│   └── client_test.go      # ✅ 16 tests, 75.3% coverage
├── broker/
│   ├── rabbitmq.go
│   └── rabbitmq_test.go    # ⏳ To be created
└── config/
    ├── config.go
    └── config_test.go       # ⏳ To be created
```

### Python Tests

Python tests use `pytest` and are located in `tests/` directories:

```
embeddings-service/
├── app/
│   └── services/
│       └── embeddings.py
└── tests/
    └── test_embeddings.py   # ⏳ To be created

gliner-service/
├── cmd/gliner-service/
│   └── tests/
│       └── test_api.py      # Existing
```

## CI/CD Integration

### GitHub Actions Workflow (example)

```yaml
name: Tests
on: [push, pull_request]
jobs:
  test-go:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-go@v4
        with:
          go-version: '1.21'
      - run: go test -v -race -cover ./...
      
  test-python:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-python@v4
        with:
          python-version: '3.11'
      - run: |
          cd embeddings-service
          pip install -r requirements.txt -r dev-requirements.txt
          pytest tests/ -v --cov=app --cov-report=xml
```

## Dependencies

### Go Test Dependencies

```bash
go get github.com/stretchr/testify/assert
go get github.com/stretchr/testify/require
go get github.com/alicebob/miniredis/v2
```

Already added to `go.mod` via `go mod tidy`.

### Python Test Dependencies

```bash
pip install pytest pytest-cov httpx
```

## Writing Tests

### Go Test Example

```go
func TestMyFunction(t *testing.T) {
    // Arrange
    input := "test"
    expected := "TEST"
    
    // Act
    result := MyFunction(input)
    
    // Assert
    assert.Equal(t, expected, result)
}
```

### Python Test Example

```python
def test_my_function():
    # Arrange
    input_data = "test"
    expected = "TEST"
    
    # Act
    result = my_function(input_data)
    
    # Assert
    assert result == expected
```

## Mocking

### Redis Mocking (Go)
Uses `miniredis/v2` for in-memory Redis simulation without Docker:

```go
mr := miniredis.RunT(t)
defer mr.Close()

client := redis.NewClient(&redis.Options{
    Addr: mr.Addr(),
})
```

### HTTP Mocking (Python)
Uses `httpx.MockTransport` for HTTP API testing:

```python
transport = httpx.MockTransport(handler)
client = httpx.Client(transport=transport)
```

## Continuous Improvement

- [ ] Achieve 70% Go coverage
- [ ] Achieve 60% Python coverage
- [ ] Add integration tests
- [ ] Add E2E tests
- [ ] Setup CI/CD pipeline
- [ ] Add performance benchmarks

---

# End-to-End Testing Guide

## Quick Start Testing

### 1. Start All Services

```bash
cd deploy/docker
docker-compose up -d
```

### 2. Verify Services Are Running

```bash
docker-compose ps
```

Expected output should show 9 services running:
- orchestrator
- extraction-worker (NEW)
- embeddings-worker
- entities-worker
- metadata-worker
- completion-worker (NEW)
- rabbitmq
- redis
- unstructured

### 3. Check Service Health

```bash
# Check orchestrator health
curl http://localhost:8080/health | jq

# Should return:
# {
#   "status": "healthy",
#   "checks": {
#     "rabbitmq": {"status": "healthy", "latency_ms": ...},
#     "redis": {"status": "healthy", "latency_ms": ...}
#   }
# }
```

## End-to-End Tests

### Test 1: Process Document from Base64

```bash
# Create a test document (plain text)
echo "Hello World! This is a test document for the IA Text Orchestrator system. It contains multiple sentences to test entity extraction and metadata analysis." | base64

# Send the document for processing
curl -X POST http://localhost:8080/v1/documents/process \
  -H "Content-Type: application/json" \
  -d '{
    "document_base64": "SGVsbG8gV29ybGQhIFRoaXMgaXMgYSB0ZXN0IGRvY3VtZW50IGZvciB0aGUgSUEgVGV4dCBPcmNoZXN0cmF0b3Igc3lzdGVtLiBJdCBjb250YWlucyBtdWx0aXBsZSBzZW50ZW5jZXMgdG8gdGVzdCBlbnRpdHkgZXh0cmFjdGlvbiBhbmQgbWV0YWRhdGEgYW5hbHlzaXMuCg=="
  }' | jq

# Expected response:
# {
#   "job_id": "1738123456789012345",
#   "status": "pending",
#   "status_url": "/v1/documents/1738123456789012345"
# }

# Save the job_id for next step
JOB_ID="<job_id_from_response>"
```

### Test 2: Monitor Job Progress

```bash
# Watch logs in real-time
docker-compose logs -f extraction-worker embeddings-worker entities-worker metadata-worker completion-worker

# Expected log sequence:
# [extraction-worker] Processing text extraction for job: <job_id>
# [extraction-worker] Stored text for job <job_id>: N characters
# [extraction-worker] Published job to queues: embeddings, entities, metadata
# [embeddings-worker] Processing embeddings for job: <job_id>
# [entities-worker] Processing entities for job: <job_id>
# [metadata-worker] Processing metadata for job: <job_id>
# [embeddings-worker] Embeddings completed for job: <job_id>
# [entities-worker] Entities completed for job: <job_id>
# [metadata-worker] Metadata completed for job: <job_id>
# [completion-worker] Job <job_id> completed steps: {extraction, embeddings, entities, metadata}
# [completion-worker] Finalizing job: <job_id>
# [completion-worker] Job <job_id> finalized and marked as completed
```

### Test 3: Check Job Status

```bash
# Check job status (should complete in 10-30 seconds)
curl http://localhost:8080/v1/documents/$JOB_ID | jq

# Expected response when completed:
# {
#   "job_id": "1738123456789012345",
#   "status": "completed",
#   "results": {
#     "text": "Hello World! This is a test document...",
#     "embeddings": [0.123, 0.456, ...],  // 1024 floats
#     "entities": [...],
#     "metadata": {
#       "word_count": 25,
#       "char_count": 142,
#       "language": "en"
#     }
#   },
#   "error": ""
# }
```

### Test 4: Verify Queue State

```bash
# Check RabbitMQ queues
docker-compose exec rabbitmq rabbitmqctl list_queues name messages consumers

# Expected output (all queues empty with consumers):
# extract_text     0  1
# embeddings       0  1
# entities         0  1
# metadata         0  1
# dead_letters     0  0
```

### Test 5: Verify Redis State

```bash
# Check Redis keys for the job
docker-compose exec redis redis-cli KEYS "orchestrator:job:$JOB_ID:*"

# Check final status
docker-compose exec redis redis-cli HGET "orchestrator:job:$JOB_ID:status" status
# Expected: "completed"
```

### Test 6: Verify Metrics

```bash
# Check Prometheus metrics
curl http://localhost:8080/metrics | grep -E "ia_text_(jobs_total|queue_depth)"
```

## Security Tests

### Test 7: SSRF Prevention

```bash
# Localhost URLs should be blocked
curl -X POST http://localhost:8080/v1/documents/process \
  -H "Content-Type: application/json" \
  -d '{"document_url": "http://localhost/admin"}' | jq

# Expected: 400 Bad Request with "localhost URLs are not allowed"
```

### Test 8: DoS Prevention

```bash
# Test oversized document (>10MB should fail)
python3 << 'EOF'
import base64
import requests

large_doc = "A" * (11 * 1024 * 1024)
encoded = base64.b64encode(large_doc.encode()).decode()

response = requests.post(
    "http://localhost:8080/v1/documents/process",
    json={"document_base64": encoded}
)

print(response.status_code)  # Should be 400
print(response.json())
EOF
```

## Success Criteria

A successful end-to-end test should demonstrate:

1. ✅ Job created successfully (status 202)
2. ✅ Text extracted and stored in Redis
3. ✅ All parallel workers process successfully
4. ✅ Completion worker detects finalization
5. ✅ Results aggregated correctly
6. ✅ Job status changes to "completed"
7. ✅ API returns complete results
8. ✅ No messages in dead letter queue
9. ✅ Metrics updated correctly
10. ✅ Security validations working (SSRF, DoS)
