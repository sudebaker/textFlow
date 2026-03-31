# IA Text Orchestrator - API Reference

## Overview

The IA Text Orchestrator is an event-driven microservices platform for document processing, featuring a REST API for job creation, status tracking, and result retrieval. The orchestrator coordinates extraction, embeddings, entity recognition, and metadata analysis through RabbitMQ and Redis.

**Base URL:** `http://localhost:8080`  
**API Version:** `v1`

---

## Authentication

Currently, the API does not require authentication. All endpoints are publicly accessible.

---

## Common Response Formats

### Success Response (200, 201, 202)
```json
{
  "job_id": "job_abc123xyz",
  "status": "pending|extracting|processing|embedding|entities|completed",
  "results": {
    "text": "extracted text...",
    "chunks": [...],
    "embeddings": {...},
    "entities": [...],
    "document_metadata": {...},
    "text_metadata": {...}
  },
  "created_at": "2025-03-16T10:30:00Z",
  "completed_at": "2025-03-16T10:35:00Z"
}
```

### Error Response (4xx, 5xx)
```json
{
  "error": "error_code",
  "detail": "Human-readable error description"
}
```

---

## Endpoints

### 1. Health Check

**Endpoint:** `GET /health`

**Description:** Check if the orchestrator service is running and healthy.

**Response:** 
```json
{
  "status": "ok"
}
```

**Example:**
```bash
curl http://localhost:8080/health
```

---

### 2. Create Job (Document Upload)

**Endpoint:** `POST /v1/documents/process`

**Description:** Create a new document processing job using base64-encoded document or URL. The orchestrator will extract text, generate embeddings, identify entities, and extract metadata.

**Request Body:**
```json
{
  "document_base64": "JVBERi0xLjQKJeLj...",  // Either this
  "document_url": "https://example.com/doc.pdf",  // Or this (mutually exclusive)
  "filename": "document.pdf"  // Optional
}
```

**Parameters:**
- `document_base64` (string, conditional): Base64-encoded document. Required if `document_url` is not provided.
- `document_url` (string, conditional): URL to the document. Required if `document_base64` is not provided. Must be a valid HTTP/HTTPS URL.
- `filename` (string, optional): Original filename for metadata tracking.

**Response (202 Accepted):**
```json
{
  "job_id": "job_xyz789abc",
  "status": "pending",
  "status_url": "http://localhost:8080/v1/documents/job_xyz789abc"
}
```

**Error Examples:**
- `400 Bad Request`: Missing required fields or invalid JSON
- `400 Bad Request`: Both `document_base64` and `document_url` provided
- `400 Bad Request`: Invalid URL or SSRF attempt detected
- `413 Payload Too Large`: Document exceeds 10MB limit
- `500 Internal Server Error`: Failed to queue job

**Example:**
```bash
# Using base64
curl -X POST http://localhost:8080/v1/documents/process \
  -H "Content-Type: application/json" \
  -d '{
    "document_base64": "JVBERi0xLjQKJeLj...",
    "filename": "report.pdf"
  }'

# Using URL
curl -X POST http://localhost:8080/v1/documents/process \
  -H "Content-Type: application/json" \
  -d '{
    "document_url": "https://example.com/doc.pdf"
  }'
```

---

### 3. Upload File (Multipart)

**Endpoint:** `POST /v1/documents/upload`

**Description:** Upload a document file directly as multipart form data. Supports PDF, text, Word, PowerPoint, Excel, CSV, JSON, and image formats.

**Request:**
```
POST /v1/documents/upload HTTP/1.1
Content-Type: multipart/form-data; boundary=----Boundary

------Boundary
Content-Disposition: form-data; name="file"; filename="document.pdf"
Content-Type: application/pdf

[binary file content]
------Boundary
Content-Disposition: form-data; name="notify_webhook"

https://example.com/webhook
------Boundary--
```

**Parameters:**
- `file` (file, required): The document file to upload. Supported types:
  - Documents: `.pdf`, `.txt`, `.doc`, `.docx`, `.ppt`, `.pptx`
  - Spreadsheets: `.xls`, `.xlsx`, `.csv`
  - Data: `.json`
  - Images: `.jpg`, `.jpeg`, `.png`
- `notify_webhook` (string, optional): Webhook URL to notify when job completes. If not provided, uses `WEBHOOK_URL` from server config.

**Constraints:**
- Maximum file size: **10MB**
- Spreadsheets: Maximum **2,000 rows** (configurable via `MAX_SPREADSHEET_ROWS`)
- Spreadsheets: Maximum **5MB** (configurable via `MAX_SPREADSHEET_SIZE_MB`)

**Response (202 Accepted):**
```json
{
  "job_id": "job_abc123def456",
  "status": "pending",
  "status_url": "http://localhost:8080/v1/documents/job_abc123def456"
}
```

**Error Examples:**
- `400 Bad Request`: No file provided or file parse error
- `400 Bad Request`: Invalid file type
- `413 Payload Too Large`: File exceeds size limit
- `400 Bad Request`: CSV/Excel exceeds row limit
- `500 Internal Server Error`: Failed to save or queue job

**Example:**
```bash
curl -X POST http://localhost:8080/v1/documents/upload \
  -F "file=@report.pdf" \
  -F "notify_webhook=https://myapp.com/webhook"
```

---

### 4. Get Job Status (Polling)

**Endpoint:** `GET /v1/documents/{job_id}`

**Description:** Poll the status of a document processing job. Returns the current pipeline step and status only. **Does not return results** — use the `/download` endpoint once the job is `completed`.

> **Note:** This endpoint is designed for lightweight polling. It will never include text, chunks, embeddings, or entities in its response. For results, use [`GET /v1/documents/{job_id}/download`](#5-download-job-results).

**Path Parameters:**
- `job_id` (string, required): The job ID returned from job creation.

**Response (200 OK):**

**Pending/Processing:**
```json
{
  "job_id": "job_xyz789abc",
  "status": "processing",
  "current_step": "embedding",
  "created_at": "2025-03-16T10:30:00Z"
}
```

**Completed:**
```json
{
  "job_id": "job_xyz789abc",
  "status": "completed",
  "current_step": "completed",
  "created_at": "2025-03-16T10:30:00Z",
  "completed_at": "2025-03-16T10:35:00Z"
}
```

**Failed:**
```json
{
  "job_id": "job_xyz789abc",
  "status": "failed",
  "current_step": "extracting",
  "error": "extraction_error",
  "created_at": "2025-03-16T10:30:00Z"
}
```

**Response Fields:**
| Field | Type | Description |
|-------|------|-------------|
| `job_id` | string | The job identifier |
| `status` | string | Overall job status (see lifecycle below) |
| `current_step` | string | The pipeline step currently executing or last executed |
| `created_at` | string | ISO 8601 timestamp of job creation |
| `completed_at` | string | ISO 8601 timestamp of completion (only when `status=completed`) |
| `error` | string | Error code (only when `status=failed`) |

**Error Examples:**
- `404 Not Found`: Job ID does not exist
- `500 Internal Server Error`: Failed to retrieve job status

**Job Status Lifecycle:**
1. `pending` — Job created, waiting for extraction
2. `extracting` — Document extraction in progress
3. `processing` — Text processing
4. `embedding` — Generating embeddings (if applicable)
5. `entities` — Extracting entities
6. `completed` — All processing complete, results available via `/download`
7. `failed` — Job failed at some stage

**Example:**
```bash
curl http://localhost:8080/v1/documents/job_xyz789abc
```

---

### 5. Download Job Results

**Endpoint:** `GET /v1/documents/{job_id}/download`

**Description:** Download the full processing results for a completed job. Returns gzip-compressed JSON containing text, chunks, embeddings, entities, and metadata. **Only available when job status is `completed`.**

> **Note:** This endpoint reads results from the filesystem. Always check job status via `GET /v1/documents/{job_id}` before calling this endpoint.

**Path Parameters:**
- `job_id` (string, required): The job ID returned from job creation.

**Query Parameters:**
- `raw` (string, optional): Set to `true` to disable gzip compression and receive plain JSON.

**Response Headers (compressed, default):**
```
Content-Encoding: gzip
Content-Disposition: attachment; filename=results_job_xyz789abc.json.gz
Content-Type: application/json
```

**Response Body (JSON — after decompression):**
```json
{
  "job_id": "job_xyz789abc",
  "status": "completed",
  "created_at": "2025-03-16T10:30:00Z",
  "completed_at": "2025-03-16T10:35:00Z",
  "text": "extracted text content...",
  "chunks": [
    {
      "chunk_id": "chunk_0",
      "text": "First paragraph...",
      "start_offset": 0,
      "end_offset": 128,
      "token_count": 25
    }
  ],
  "embeddings": {
    "chunk_0": [0.123, 0.456, "..."]
  },
  "entities": [
    {
      "text": "John Doe",
      "label": "PERSON",
      "confidence": 0.95,
      "chunk_id": "chunk_0",
      "start": 10,
      "end": 18
    }
  ],
  "document_metadata": {
    "mime_type": "application/pdf",
    "size_bytes": 1024000,
    "pages": 10,
    "filename": "report.pdf",
    "author": "Jane Smith",
    "title": "Q1 Report",
    "creation_date": "2025-01-15",
    "sha256": "abc123def456..."
  },
  "text_metadata": {
    "language": "en",
    "word_count": 5432
  }
}
```

**Error Examples:**
- `404 Not Found`: Job ID does not exist or results file not found
- `425 Too Early` / `400 Bad Request`: Job is not yet completed
- `500 Internal Server Error`: Failed to read results file

**Example:**
```bash
# Download compressed results (default)
curl -o results.json.gz http://localhost:8080/v1/documents/job_xyz789abc/download
gunzip results.json.gz

# Download uncompressed
curl http://localhost:8080/v1/documents/job_xyz789abc/download?raw=true
```

---

## Data Models

### Job Status
Possible values: `pending`, `extracting`, `processing`, `embedding`, `entities`, `completed`, `failed`

### Entity Object
```json
{
  "text": "entity text",
  "label": "ENTITY_TYPE",
  "confidence": 0.95,
  "chunk_id": "chunk_0",
  "start": 10,
  "end": 25
}
```

**Entity Labels:**

**From Regex Extractor:**
- `EMAIL` — Email addresses
- `PHONE` — Phone numbers
- `CREDIT_CARD` — Credit card numbers
- `IBAN_ES` — Spanish IBAN
- `IBAN_INTL` — International IBAN
- `DNI_ES` — Spanish National ID
- `NIF_ES` — Spanish Tax ID (individuals)
- `CIF_ES` — Spanish Tax ID (companies)
- `VAT_ES` — Spanish VAT number
- `LICENSE_PLATE_ES` — Spanish license plates
- `CRYPTO_BTC_ADDRESS` — Bitcoin addresses
- `GEOLOCATION_DD` — GPS coordinates (decimal degrees)
- `GEOLOCATION_DMS` — GPS coordinates (degrees/minutes/seconds)
- `URL` — URLs
- `IPV4` — IPv4 addresses
- `MAC_ADDRESS` — MAC addresses
- `PASSPORT_EU` — EU passports
- `HASHTAG` — Social hashtags
- `SOCIAL_MENTION` — Social media mentions (@mentions)
- `DATE` — Dates

**From GLiNER ML Model:**
- `PERSON` — Person names
- `ORGANIZATION` — Organization names
- `LOCATION` — Geographic locations
- `MONEY` — Monetary amounts

### Chunk Object
```json
{
  "chunk_id": "chunk_0",
  "text": "chunk text content",
  "start_offset": 0,
  "end_offset": 256,
  "token_count": 50
}
```

### Metadata Object
```json
{
  "mime_type": "application/pdf",
  "size_bytes": 1024000,
  "pages": 10,
  "filename": "document.pdf",
  "author": "Jane Doe",
  "title": "Document Title",
  "creation_date": "2025-01-15",
  "sha256": "abc123def456..."
}
```

---

## Configuration

### Server Configuration

Environment variables (set in `docker-compose.yml` or `.env`):

| Variable | Default | Description |
|----------|---------|-------------|
| `HTTP_PORT` | 8080 | HTTP server port |
| `LOG_LEVEL` | info | Logging level (debug, info, warn, error) |
| `JOB_TIMEOUT` | 5m | Maximum time to process a single job |
| `JOB_TTL` | 24h | Time to keep completed job results in Redis |
| `MAX_RETRIES` | 3 | Max retries for failed pipeline stages |
| `RETRY_DELAY` | 1s | Delay between retries |
| `ENTITY_TYPES` | PERSON,ORGANIZATION,LOCATION | Default entity types to extract |
| `MAX_SPREADSHEET_ROWS` | 2000 | Max rows allowed in CSV/Excel |
| `MAX_SPREADSHEET_SIZE_MB` | 5 | Max size for spreadsheet files |
| `UPLOAD_PATH` | /app/data/uploads | Where to store uploaded files |
| `RESULTS_PATH` | /app/data/results | Where to store result files |

### Infrastructure Services

- **RabbitMQ:** `RABBITMQ_URL` (default: `amqp://guest:guest@rabbitmq:5672`) <!-- NOTE: Do not use guest:guest in production. See .env.example for guidance. -->
- **Redis:** `REDIS_URL` (default: `redis://redis:6379`)
- **Docling (Extraction):** `DOCLING_URL` (default: `http://localhost:8000`)
- **Regex Entity Extractor:** `REGEX_ENTITY_EXTRACTOR_URL` (default: `http://regex-entity-extractor:8081`)

---

## Rate Limiting

No rate limiting is currently implemented. Be mindful of system resources when submitting multiple large documents simultaneously.

---

## Webhook Notifications

When a job completes, the orchestrator can notify a webhook endpoint with the final results.

**Webhook Request:**
```
POST {webhook_url}
Content-Type: application/json

{
  "job_id": "job_xyz789abc",
  "status": "completed",
  "results": { ... }  // Full job results object
}
```

---

## Error Handling

### Common Errors

| Code | Error | Cause |
|------|-------|-------|
| 400 | invalid_request | Malformed JSON or missing required fields |
| 400 | invalid_input | Document validation failed (e.g., SSRF attempt) |
| 400 | invalid_file_type | Uploaded file type not supported |
| 413 | file_too_large | File exceeds size limit (10MB) |
| 404 | not_found | Job ID does not exist |
| 500 | internal_error | Server error (check logs) |

---

## Example Workflows

### Workflow 1: Process PDF by URL

```bash
# 1. Create job
JOB=$(curl -s -X POST http://localhost:8080/v1/documents/process \
  -H "Content-Type: application/json" \
  -d '{"document_url": "https://example.com/report.pdf"}' | jq -r '.job_id')

echo "Job ID: $JOB"

# 2. Poll for completion (status only — no results here)
while true; do
  RESPONSE=$(curl -s http://localhost:8080/v1/documents/$JOB)
  STATUS=$(echo $RESPONSE | jq -r '.status')
  STEP=$(echo $RESPONSE | jq -r '.current_step')
  if [ "$STATUS" = "completed" ]; then
    echo "Job completed!"
    break
  elif [ "$STATUS" = "failed" ]; then
    echo "Job failed at step: $STEP"
    break
  fi
  echo "Status: $STATUS, current step: $STEP"
  sleep 5
done

# 3. Download results (only after status=completed)
curl -s http://localhost:8080/v1/documents/$JOB/download | jq '.entities'
```

### Workflow 2: Upload File with Results

```bash
# Upload file
RESPONSE=$(curl -s -X POST http://localhost:8080/v1/documents/upload \
  -F "file=@myfile.xlsx" \
  -F "notify_webhook=https://myapp.com/webhook")

JOB=$(echo $RESPONSE | jq -r '.job_id')

# Poll until done (max 90 seconds for spreadsheets)
for i in {1..45}; do
  STATUS_RESP=$(curl -s http://localhost:8080/v1/documents/$JOB)
  STATUS=$(echo $STATUS_RESP | jq -r '.status')
  
  if [ "$STATUS" = "completed" ]; then
    echo "Extraction complete!"
    # Download full results from /download endpoint
    curl -s http://localhost:8080/v1/documents/$JOB/download | jq '.entities'
    exit 0
  fi
  
  sleep 2
done

echo "Timeout waiting for job"
```

---

## Monitoring

### Health Check Endpoint

```bash
curl -s http://localhost:8080/health | jq
```

### Metrics (Prometheus)

Metrics are available at (when monitoring is enabled):
- `/metrics` — Prometheus format

Common metrics:
- `jobs_total{status,type}` — Total jobs by status and type
- `jobs_in_progress` — Currently processing jobs
- `job_duration_seconds` — Job processing time
- `queue_publish_total{queue}` — Messages published to queue

---

## Batch Processing

### Create Batch

**Endpoint:** `POST /v1/documents/batch`

**Description:** Create a batch job to process multiple documents in parallel.

**Request Body:**
```json
{
  "documents": [
    {
      "text": "Texto del primer documento",
      "filename": "doc1.txt",
      "metadata": {"author": "user1"}
    },
    {
      "text": "Texto del segundo documento",
      "filename": "doc2.txt",
      "metadata": {"author": "user2"}
    }
  ],
  "max_concurrency": 10,
  "webhook_url": "https://example.com/webhook",
  "webhook_secret": "my-secret"
}
```

**Parameters:**
- `documents` (array, required): List of documents to process (max 100)
- `documents[].text` (string, required): Text content of the document
- `documents[].filename` (string, optional): Original filename
- `documents[].metadata` (object, optional): Custom metadata
- `max_concurrency` (int, optional): Max parallel jobs (1-50, default: 10)
- `webhook_url` (string, optional): Webhook URL for batch completion
- `webhook_secret` (string, optional): Secret for webhook signature

**Response (202 Accepted):**
```json
{
  "batch_id": "batch_abc123",
  "total": 2,
  "jobs": [
    {"id": "job_001", "filename": "doc1.txt", "status": "pending"},
    {"id": "job_002", "filename": "doc2.txt", "status": "pending"}
  ],
  "status_url": "/v1/batches/batch_abc123/status",
  "created_at": "2025-03-16T10:30:00Z"
}
```

**Example:**
```bash
curl -X POST http://localhost:8080/v1/documents/batch \
  -H "Content-Type: application/json" \
  -d '{
    "documents": [
      {"text": "First document text", "filename": "doc1.txt"},
      {"text": "Second document text", "filename": "doc2.txt"}
    ],
    "webhook_url": "https://myapp.com/batch-webhook"
  }'
```

---

### Get Batch Status

**Endpoint:** `GET /v1/batches/{batch_id}/status`

**Description:** Get the status of a batch job.

**Path Parameters:**
- `batch_id` (string, required): The batch ID returned from batch creation.

**Response (200 OK):**
```json
{
  "batch_id": "batch_abc123",
  "status": "completed",
  "total": 2,
  "completed": 2,
  "failed": 0,
  "pending": 0,
  "jobs": [
    {"id": "job_001", "status": "completed"},
    {"id": "job_002", "status": "completed"}
  ],
  "created_at": "2025-03-16T10:30:00Z"
}
```

**Batch Status Values:**
- `running`: Batch is still processing
- `completed`: All jobs completed successfully
- `partial`: Some jobs failed
- `failed`: All jobs failed

**Example:**
```bash
curl http://localhost:8080/v1/batches/batch_abc123/status
```

---

## Streaming (SSE)

### Stream Job Events

**Endpoint:** `GET /v1/jobs/{job_id}/stream`

**Description:** Subscribe to real-time job status updates via Server-Sent Events (SSE).

**Path Parameters:**
- `job_id` (string, required): The job ID to stream.

**Response (200 OK):**
```
Content-Type: text/event-stream

event: job_pending
data: {"job_id":"job_abc","status":"pending","timestamp":"2025-03-16T10:30:00Z"}

event: job_extracting
data: {"job_id":"job_abc","status":"extracting","progress":0.5,"timestamp":"2025-03-16T10:30:05Z"}

event: job_completed
data: {"job_id":"job_abc","status":"completed","timestamp":"2025-03-16T10:35:00Z"}
```

**Event Types:**
- `job_pending`: Job created, waiting for processing
- `job_extracting`: Text extraction in progress
- `job_processing`: Processing (embeddings, entities)
- `job_completed`: Job completed successfully
- `job_failed`: Job failed with error

**Heartbeat:**
The server sends periodic heartbeat comments (`: heartbeat\n\n`) every 30 seconds to keep the connection alive.

**Example:**
```bash
curl -N http://localhost:8080/v1/jobs/job_abc123/stream
```

---

## Webhooks

### Per-Request Webhooks

When creating a job, you can specify a webhook URL to receive notifications when the job completes:

**Request:**
```bash
curl -X POST http://localhost:8080/v1/documents/process \
  -H "Content-Type: application/json" \
  -d '{
    "document_base64": "...",
    "webhook_url": "https://myapp.com/webhook",
    "webhook_secret": "my-secret"
  }'
```

**Webhook Payload:**
```json
{
  "job_id": "job_abc123",
  "status": "completed",
  "completed_at": "2025-03-16T10:35:00Z",
  "results": {
    "text": "...",
    "chunks": [...],
    "entities": {...}
  }
}
```

**Signature Verification:**
If `webhook_secret` is provided, the webhook includes an `X-Signature-256` header:
```
X-Signature-256: sha256=<hmac-hex>
```

Verify with:
```python
import hmac
import hashlib

def verify_signature(payload: bytes, secret: str, signature: str) -> bool:
    expected = 'sha256=' + hmac.new(
        secret.encode(),
        payload,
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)
```

### Batch Webhooks

Batch jobs support webhooks that fire when all jobs in the batch complete:

```bash
curl -X POST http://localhost:8080/v1/documents/batch \
  -H "Content-Type: application/json" \
  -d '{
    "documents": [...],
    "webhook_url": "https://myapp.com/batch-webhook",
    "webhook_secret": "batch-secret"
  }'
```

---

## Compression

### Gzip-Compressed Downloads (Default)

The `/v1/documents/{job_id}/download` endpoint **compresses embeddings by default** using gzip.

**Request (default - compressed):**
```bash
curl http://localhost:8080/v1/documents/job_abc123/download
```

**Request (raw/uncompressed - opt-out):**
```bash
curl http://localhost:8080/v1/documents/job_abc123/download?raw=true
```

**Response Headers (compressed):**
```
Content-Encoding: gzip
Content-Disposition: attachment; filename=results_job_abc123.json.gz
```

**Response Fields:**
| Field | Type | Description |
|-------|------|-------------|
| `embedding_compressed` | string | Base64-encoded gzip-compressed float32 array (little-endian) |
| `compression` | string | Always `"gzip"` when compressed |

**Decompression Example (Python):**
```python
import base64, gzip, struct, json

def decompress_embeddings(embedded: str) -> list[float]:
    compressed = base64.b64decode(embedded)
    raw_bytes = gzip.decompress(compressed)
    count = len(raw_bytes) // 4
    return struct.unpack(f'<{count}f', raw_bytes)

results = requests.get(f'{API}/v1/documents/{job_id}/download').json()
for chunk in results['chunks']:
    if 'embedding_compressed' in chunk:
        chunk['embeddings'] = decompress_embeddings(chunk['embedding_compressed'])
```

**Benefits:**
- 70-90% reduction in transfer size for large embeddings
- Faster download times
- Reduced bandwidth costs

---

## Changelog

### v1.0.0 (2025-03-16)
- Initial API release
- Support for PDF, Excel, CSV, JSON, Word, PowerPoint, images
- Entity extraction (regex + ML)
- Embeddings generation
- Document metadata extraction

---

## Support

For issues or questions:
1. Check server logs: `docker logs ia-text-orchestrator`
2. Review worker logs: `docker logs ia-text-entities-worker`, etc.
3. Verify Redis/RabbitMQ are healthy: `make infra-status`
4. File an issue: GitHub Issues

