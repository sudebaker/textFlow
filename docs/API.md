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

### 4. Get Job Status & Results

**Endpoint:** `GET /v1/documents/{job_id}`

**Description:** Poll the status of a document processing job and retrieve results once completed.

**Path Parameters:**
- `job_id` (string, required): The job ID returned from job creation.

**Response (200 OK):**

**Pending/Processing:**
```json
{
  "job_id": "job_xyz789abc",
  "status": "processing",
  "created_at": "2025-03-16T10:30:00Z"
}
```

**Completed:**
```json
{
  "job_id": "job_xyz789abc",
  "status": "completed",
  "results": {
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
      "chunk_0": [0.123, 0.456, ...]
    },
    "entities": [
      {
        "text": "John Doe",
        "label": "PERSON",
        "confidence": 0.95,
        "chunk_id": "chunk_0",
        "start": 10,
        "end": 18
      },
      {
        "text": "john@example.com",
        "label": "EMAIL",
        "confidence": 1.0,
        "chunk_id": "chunk_0",
        "start": 50,
        "end": 65
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
  },
  "created_at": "2025-03-16T10:30:00Z",
  "completed_at": "2025-03-16T10:35:00Z"
}
```

**Failed:**
```json
{
  "job_id": "job_xyz789abc",
  "status": "failed",
  "error": "extraction_error",
  "created_at": "2025-03-16T10:30:00Z"
}
```

**Error Examples:**
- `404 Not Found`: Job ID does not exist
- `500 Internal Server Error`: Failed to retrieve job status

**Job Status Lifecycle:**
1. `pending` — Job created, waiting for extraction
2. `extracting` — Document extraction in progress
3. `processing` — Text processing
4. `embedding` — Generating embeddings (if applicable)
5. `entities` — Extracting entities
6. `completed` — All processing complete
7. `failed` — Job failed at some stage

**Example:**
```bash
curl http://localhost:8080/v1/documents/job_xyz789abc
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

- **RabbitMQ:** `RABBITMQ_URL` (default: `amqp://guest:guest@rabbitmq:5672`)
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

# 2. Poll for completion
while true; do
  STATUS=$(curl -s http://localhost:8080/v1/documents/$JOB | jq -r '.status')
  if [ "$STATUS" = "completed" ]; then
    echo "Job completed!"
    break
  elif [ "$STATUS" = "failed" ]; then
    echo "Job failed!"
    break
  fi
  echo "Status: $STATUS"
  sleep 5
done

# 3. Retrieve results
curl -s http://localhost:8080/v1/documents/$JOB | jq '.results'
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
  RESULT=$(curl -s http://localhost:8080/v1/documents/$JOB)
  STATUS=$(echo $RESULT | jq -r '.status')
  
  if [ "$STATUS" = "completed" ]; then
    echo "Extraction complete!"
    echo $RESULT | jq '.results.entities'
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

