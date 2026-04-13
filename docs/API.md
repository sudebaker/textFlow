# textFlow - API Reference

## Overview

textFlow is an event-driven microservices platform for document processing, featuring a REST API for job creation, status tracking, and result retrieval. The orchestrator coordinates extraction, embeddings, entity recognition, and metadata analysis through RabbitMQ and Redis.

**Base URL:** `http://localhost:8080`  
**API Version:** `v1`

---

## Authentication

Currently, the API does not require authentication. All endpoints are publicly accessible.

---

## Common Response Formats

### Success Response (200, 202)
```json
{
  "job_id": "job_abc123xyz",
  "status": "pending|extracting|processing|embedding|entities|inferences|completed",
  "created_at": "2025-03-16T10:30:00Z"
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
  "status": "healthy",
  "timestamp": "2025-03-16T10:30:00Z",
  "service": "textflow",
  "version": "1.0.0",
  "uptime": "2h30m",
  "memory_usage": "Check /metrics for detailed memory metrics",
  "checks": {
    "redis": { "status": "healthy", "latency_ms": 1 },
    "rabbitmq": { "status": "healthy", "latency_ms": 3 }
  }
}
```

Status values: `healthy`, `degraded`, `down`. HTTP 200 for healthy/degraded, 503 for down.

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
  "filename": "document.pdf",  // Optional
  "features": ["inferences"],  // Optional: enable extra pipeline stages
  "webhook_url": "https://myapp.com/webhook",  // Optional
  "webhook_secret": "my-secret"  // Optional
}
```

**Parameters:**
- `document_base64` (string, conditional): Base64-encoded document. Required if `document_url` is not provided.
- `document_url` (string, conditional): URL to the document. Required if `document_base64` is not provided. Must be a valid HTTP/HTTPS URL.
- `filename` (string, optional): Original filename for metadata tracking.
- `features` (array, optional): Extra pipeline stages to enable. Currently supported: `"inferences"` (requires vLLM/inference-worker).
- `webhook_url` (string, optional): URL to notify when job completes.
- `webhook_secret` (string, optional): Secret for `X-Signature-256` HMAC verification.

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
- `400 Bad Request`: Document exceeds 10MB limit
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

**Description:** Upload a document, audio, image, or spreadsheet file directly as multipart form data. The orchestrator will process the file type automatically, extracting text and metadata, generating embeddings, identifying entities, and optionally running inferences or diarization.

**Supported File Types:**

| Category | Extensions | Max Size | Notes |
|----------|-----------|----------|-------|
| **Documents** | `.pdf`, `.txt`, `.doc`, `.docx`, `.ppt`, `.pptx` | 10 MB | Text extraction via Docling |
| **Spreadsheets** | `.xls`, `.xlsx`, `.csv`, `.json` | 5 MB | Flattened to text with max 2,000 rows |
| **Images** | `.jpg`, `.jpeg`, `.png`, `.webp`, `.bmp` | 10 MB | Analysis via Multimodal LLM |
| **Audio** | `.mp3`, `.wav`, `.m4a`, `.ogg`, `.flac` | 500 MB | Transcription via Whisper; supports diarization |

**Request:**
```
POST /v1/documents/upload HTTP/1.1
Content-Type: multipart/form-data; boundary=----Boundary

------Boundary
Content-Disposition: form-data; name="file"; filename="document.pdf"
Content-Type: application/pdf

[binary file content]
------Boundary
Content-Disposition: form-data; name="features"

inferences
------Boundary
Content-Disposition: form-data; name="diarize"

true
------Boundary
Content-Disposition: form-data; name="notify_webhook"

https://example.com/webhook
------Boundary--
```

**Parameters:**
- `file` (file, required): The file to upload. See **Supported File Types** table above.
- `features` (string, optional): Comma-separated list of extra pipeline features. Currently supported:
   - `inferences` — Generate micro-inferences from extracted text using inference-worker (requires vLLM).
   - Max features per job and max characters per name are configurable (see **Data Constraints** section). Invalid features are silently ignored with a warning.
- `diarize` (boolean, optional): **Audio files only.** When `true`, identifies speakers in audio transcription via Whisper's diarization. Default: `false`. Ignored for non-audio files.
- `notify_webhook` (string, optional): Webhook URL to notify when job completes. If not provided, uses `WEBHOOK_URL` from server config.

**Constraints:**
- **Documents:** Maximum 10MB (configurable via `MAX_DOCUMENT_SIZE_MB`)
- **Spreadsheets:** Maximum 5MB (configurable via `MAX_SPREADSHEET_SIZE_MB`), max 2,000 rows (configurable via `MAX_SPREADSHEET_ROWS`)
- **Images:** Maximum 10MB
- **Audio:** Maximum 500MB (configurable via `MAX_AUDIO_SIZE_MB`)
- **Rate Limiting:** 100 requests per second per IP; burst of 10 requests allowed.

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
- `400 Bad Request`: Invalid file type (e.g., `.exe` upload)
- `400 Bad Request`: File exceeds size limit (`file_too_large`)
- `400 Bad Request`: CSV/Excel exceeds row limit
- `429 Too Many Requests`: Rate limit exceeded (100 req/sec per IP)
- `500 Internal Server Error`: Failed to save or queue job

**Examples:**

**Upload image with inferences (requires vLLM):**
```bash
curl -X POST http://localhost:8080/v1/documents/upload \
  -F "file=@screenshot.png" \
  -F "features=inferences" \
  -F "notify_webhook=https://myapp.com/webhook"
```

**Upload audio with diarization (speaker identification):**
```bash
curl -X POST http://localhost:8080/v1/documents/upload \
  -F "file=@meeting_recording.mp3" \
  -F "diarize=true" \
  -F "notify_webhook=https://myapp.com/webhook"
```

**Upload audio with both diarization and inferences:**
```bash
curl -X POST http://localhost:8080/v1/documents/upload \
  -F "file=@conference.wav" \
  -F "diarize=true" \
  -F "features=inferences" \
  -F "notify_webhook=https://myapp.com/webhook"
```

**Upload PDF (basic processing, no extra features):**
```bash
curl -X POST http://localhost:8080/v1/documents/upload \
  -F "file=@report.pdf"
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
  "steps": {
    "extraction": "completed",
    "embeddings": "completed",
    "entities": "completed",
    "metadata": "completed"
  }
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
| `steps` | object | Per-stage status map (`processing` or `completed`). Only present while processing or on completion. |
| `error` | string | Error code (only when `status=failed`) |

**Error Examples:**
- `404 Not Found`: Job ID does not exist
- `500 Internal Server Error`: Failed to retrieve job status

**Job Status Lifecycle:**

All jobs follow the same pipeline stages regardless of file type (documents, audio, images, spreadsheets):

1. `pending` — Job created, waiting for first stage
2. `extracting` — Extracting content from file (PDF text, audio transcription, image analysis, spreadsheet parsing)
3. `processing` — Not currently used; reserved for future compatibility
4. `embedding` — Generating text embeddings (BAAI/bge-m3 model)
5. `entities` — Extracting named entities and PII (GLiNER model + regex patterns)
6. `inferences` — *(Optional)* Generating inference results (only if `features: ["inferences"]` was set in request)
7. `metadata` — Extracting and enriching document metadata
8. `completed` — All processing complete, results available via `/download`
9. `failed` — Job failed at any stage; check `error` field in response

**Pipeline by File Type:**

| File Type | Pipeline Stages | Notes |
|-----------|-----------------|-------|
| **Documents** (PDF, DOCX, etc.) | extracting → embedding → entities → [inferences] → metadata → completed | Docling extracts text, then standard NLP pipeline |
| **Audio** (MP3, WAV, etc.) | extracting (transcription) → embedding → entities → [inferences] → metadata → completed | Whisper transcribes audio; optional diarization output. Diarization output is included in extracted text with speaker labels. |
| **Images** (JPG, PNG, etc.) | extracting (analysis) → embedding → entities → [inferences] → metadata → completed | Multimodal LLM analyzes image content, produces description. Description is processed through embedding/entity stages. |
| **Spreadsheets** (CSV, XLSX, etc.) | extracting → embedding → entities → [inferences] → metadata → completed | Rows flattened into text blocks (max 2,000 rows), then standard pipeline |

**Status Transitions:**
```
pending → extracting → embedding → entities → [inferences] → metadata → completed
                             ↓
                           failed (at any stage)
```

**Response Format for Different Stages:**

When job is `extracting`:
```json
{"status": "extracting", "current_step": "extracting"}
```

When job is processing:
```json
{"status": "embedding", "current_step": "embedding"}
```

When job is completed:
```json
{
  "status": "completed",
  "current_step": "completed",
  "steps": {
    "extraction": "completed",
    "embeddings": "completed",
    "entities": "completed",
    "metadata": "completed"
  }
}
```

When job fails:
```json
{
  "status": "failed",
  "current_step": "embedding",
  "error": "embedding_error",
  "detail": "BAAI model failed to generate embeddings"
}
```

**Example:**
```bash
curl http://localhost:8080/v1/documents/job_xyz789abc
```

---

### 4a. Features Parameter

**Overview:** The `features` parameter enables optional processing stages beyond the standard pipeline. It is passed during job creation (in both `/v1/documents/upload` and `/v1/documents/process` endpoints) and causes the job to transition through additional stages before completion.

**Currently Supported Features:**

#### `inferences`
Generates micro-inferences (short, actionable insights) from the extracted text using an inference model (typically vLLM).

**Requirements:**
- `inference-worker` service must be running
- vLLM with a suitable language model (e.g., Mistral-7B, Llama-2-13B)
- The orchestrator must be able to reach the inference-worker via RabbitMQ

**Pipeline Impact:**
- Adds `inferences` stage after `entities`
- Job flow becomes: `extracting → embedding → entities → **inferences** → metadata → completed`
- Increases total job time by 5–15 seconds depending on text length

**Output Format:**
When job completes with `features: ["inferences"]`, the `/v1/documents/{job_id}/download` response includes an `inferences` array:

```json
{
  "job_id": "job_xyz789abc",
  "inferences": [
    {
      "text": "Document discusses quarterly revenue growth of 15%",
      "type": "insight",
      "confidence": 0.92
    },
    {
      "text": "Three key risks identified: supply chain, market competition, regulatory changes",
      "type": "summary",
      "confidence": 0.88
    }
  ]
}
```

**Feature Validation:**
- Maximum **10 features per job** (configurable via `MAX_FEATURES_PER_JOB` env var)
- Maximum **50 characters per feature name** (configurable via `MAX_FEATURE_NAME_LENGTH` env var)
- Feature names are **normalized to lowercase** and automatically **deduplicated**
- Invalid feature names (not in whitelist) are **silently ignored** with a warning logged + metric increment
- If **all features are invalid**, job processes normally without the optional stages
- If **feature limits are exceeded** (too many features or too long name), request returns **400 Bad Request**
- Features are validated and stored in Redis **before** the job is marked as created (ensuring data consistency)

**Examples:**

**Request with inferences (will add inferences stage):**
```bash
curl -X POST http://localhost:8080/v1/documents/upload \
  -F "file=@document.pdf" \
  -F "features=inferences"
```

**Request with invalid feature (ignored, job processes normally):**
```bash
curl -X POST http://localhost:8080/v1/documents/upload \
  -F "file=@document.pdf" \
   -F "features=nonexistent_feature"
# Output: warning logged, job runs without extra stages
```

**Feature Validation Errors:**

When feature validation fails, the API returns a **400 Bad Request** response:

```json
{
  "error": "invalid_features",
  "detail": "too many features: 15 requested, max 10 allowed"
}
```

Common error scenarios:
| Scenario | Response | Resolution |
|----------|----------|-----------|
| Too many features (>10) | `400 Bad Request` | Reduce number of features |
| Feature name too long (>50 chars) | `400 Bad Request` | Use shorter feature names |
| Invalid feature name | Warning logged, job proceeds | Feature is silently ignored; valid features are processed |
| Duplicate features | Warning logged, feature deduplicated | Feature is included once (no duplicates) |

**Monitoring:**

Invalid features are tracked in the Prometheus metric `ia_text_invalid_features_total`:

```
ia_text_invalid_features_total{reason="unknown_feature"} 5
ia_text_invalid_features_total{reason="too_long"} 2
ia_text_invalid_features_total{reason="duplicate"} 3
ia_text_invalid_features_total{reason="too_many"} 0
```

---

### 4b. Diarize Parameter

**Overview:** The `diarize` parameter enables speaker identification in audio transcription. It is only used for audio files and is ignored for other file types.

**Supported File Types:**
- `.mp3`, `.wav`, `.m4a`, `.ogg`, `.flac`

**When to Use:**
- Transcribing meetings, interviews, podcasts, or conference calls
- Identifying which speaker said what
- Extracting per-speaker statistics

**How It Works:**
1. **Whisper Transcription:** Audio is transcribed to text via Whisper
2. **Diarization:** Speaker identification is applied (marking speaker turns in transcript)
3. **Output:** Transcript includes speaker labels like `[Speaker 1]`, `[Speaker 2]`, etc.
4. **Downstream Processing:** Speaker-labeled text flows through embedding, entity extraction, and optional inference stages

**Output Format:**
When audio is uploaded with `diarize=true`, the extracted text in `/v1/documents/{job_id}/download` includes speaker labels:

```json
{
  "text": "[Speaker 1]: Good morning, everyone. Let's start the quarterly review. [Speaker 2]: Sure. [Speaker 1]: Our revenue is up 15% compared to last quarter..."
}
```

**Limitations:**
- Diarization works best with 2–5 speakers
- Background noise may affect speaker identification accuracy
- Overlapping speech may not be handled perfectly

**Example:**

**Transcribe audio with speaker identification:**
```bash
curl -X POST http://localhost:8080/v1/documents/upload \
  -F "file=@meeting_recording.mp3" \
  -F "diarize=true"
```

**Transcribe audio without diarization (standard transcription):**
```bash
curl -X POST http://localhost:8080/v1/documents/upload \
  -F "file=@meeting_recording.mp3" \
  -F "diarize=false"
```

---

### 5. Download Job Results

**Endpoint:** `GET /v1/documents/{job_id}/download`

**Description:** Download the full processing results for a completed job. Returns gzip-compressed JSON containing text, chunks, embeddings, entities, and metadata. **Only available when job status is `completed`.**

> **Note:** This endpoint reads results from the filesystem. Always check job status via `GET /v1/documents/{job_id}` before calling this endpoint.

**Path Parameters:**
- `job_id` (string, required): The job ID returned from job creation.

**Query Parameters:**
- `compression` (string, optional): Set to `raw` to disable gzip compression and receive plain JSON.

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

## External Service Requirements

The orchestrator depends on external services for different processing stages. All services can be **optionally air-gapped** (run without internet) by mounting pre-downloaded model files.

**Required Services:**

| Service | Purpose | Input | Output | Notes |
|---------|---------|-------|--------|-------|
| **Docling** | Document extraction | PDF, DOCX, PPTX, images | Extracted text, chunks, markdown | HTTP service; handles Documents category |
| **Whisper** (via inference-worker) | Audio transcription | MP3, WAV, M4A, OGG, FLAC | Transcript text, optionally with diarization | OpenAI Whisper; handles Audio category |
| **Multimodal LLM** (via inference-worker) | Image analysis | JPG, PNG, WEBP, BMP | Image description/analysis text | E.g., LLaVA, Claude Vision, GPT-4V; handles Images category |
| **BAAI/bge-m3** | Text embedding generation | Text chunks | 384-dimensional vectors | Embedding model; runs in embeddings-worker; air-gapped compatible |
| **GLiNER** | Named entity recognition | Text | Entity spans with labels and confidence | NER model; runs in entities-worker; air-gapped compatible; offline-critical |
| **Regex Entity Extractor** | PII/Pattern extraction | Text | Regex-matched entities (EMAIL, PHONE, etc.) | HTTP service; runs in entities-worker; air-gapped compatible |
| **vLLM** (optional) | Inference generation | Text | Micro-inferences/insights | Only used when `features: ["inferences"]` is set; requires inference-worker |

**Request/Response Contract (Agnostic to Implementation):**

These specifications define what each service MUST receive and return. Specific implementations (vLLM, Replicate, local models, etc.) are interchangeable as long as they respect these contracts.

**1. Docling (Document Extraction)**
```
Input:  File (PDF, DOCX, PPTX, etc.) or file URL
Output: {
  "text": "full extracted text",
  "chunks": [{"text": "...", "page": 1, "..."}],
  "metadata": {"pages": 10, "title": "...", "..."}
}
```

**2. Whisper (Audio Transcription)**
```
Input:  Audio file (MP3, WAV, etc.), optional diarize: bool
Output: {
  "text": "Transcribed text, optionally with [Speaker 1]: labels",
  "language": "en",
  "confidence": 0.95
}
```

**3. Multimodal LLM (Image Analysis)**
```
Input:  Image file (JPG, PNG, etc.) + system prompt
Output: {
  "description": "Natural language description of image",
  "objects": ["list", "of", "detected", "objects"],
  "text_in_image": "Any text visible in the image"
}
```

**4. BAAI/bge-m3 (Embeddings)**
```
Input:  Text chunk
Output: [0.123, 0.456, ..., 0.789]  # 384-dim float array
```

**5. GLiNER (NER)**
```
Input:  Text
Output: [
  {"entity": "John Doe", "label": "PERSON", "confidence": 0.95, "start": 0, "end": 8},
  ...
]
```

**6. Regex Entity Extractor (PII)**
```
Input:  Text
Output: {
  "EMAIL": ["user@example.com", ...],
  "PHONE": ["+1-555-1234", ...],
  "CREDIT_CARD": ["****-****-****-1234", ...],
  ...
}
```

**7. vLLM (Inferences) — Optional**
```
Input:  {
  "text": "...",
  "prompt": "Extract 2-3 key insights from this text"
}
Output: {
  "inferences": [
    "Insight 1",
    "Insight 2",
    ...
  ]
}
```

**Air-Gapped Deployment:**
For offline use, all services except Docling can run without internet. Pre-download models and mount them:
- BAAI/bge-m3 → `embeddings-worker`
- GLiNER → `entities-worker`
- Whisper → inference worker
- vLLM models → inference worker

See `AGENTS.md` → "Air-Gapped Deployment" for detailed setup.

---

## Rate Limits & Constraints

**Request Rate Limits:**
- **100 requests per second** per IP address (configurable)
- **Burst allowance:** 10 requests (token bucket algorithm)
- **Exceeding limit:** Returns `429 Too Many Requests` with `Retry-After` header

**File Size Constraints:**

| File Type | Max Size | Configurable Via |
|-----------|----------|------------------|
| Documents (PDF, DOCX, etc.) | 10 MB | `MAX_DOCUMENT_SIZE_MB` |
| Spreadsheets (CSV, XLSX) | 5 MB | `MAX_SPREADSHEET_SIZE_MB` |
| Images (JPG, PNG, etc.) | 10 MB | `MAX_DOCUMENT_SIZE_MB` |
| Audio (MP3, WAV, etc.) | 500 MB | `MAX_AUDIO_SIZE_MB` |

**Data Constraints:**

| Constraint | Limit | Configurable Via |
|-----------|-------|------------------|
| Spreadsheet rows | 2,000 rows | `MAX_SPREADSHEET_ROWS` |
| Features per job | 10 features | `MAX_FEATURES_PER_JOB` |
| Feature name length | 50 characters | `MAX_FEATURE_NAME_LENGTH` |
| Job TTL (time to keep results) | 24 hours | `JOB_TTL` |
| Job timeout | 5 minutes | `JOB_TIMEOUT` |
| Concurrent jobs per orchestrator | Unlimited | System resources |

**Response Time Estimates** (rough, depends on file size and server load):
- **Documents (10MB PDF):** 10–30 seconds
- **Audio (1 hour):** 30–60 seconds (transcription time varies)
- **Images (10MB):** 5–15 seconds
- **Spreadsheets (500 rows):** 5–10 seconds
- **With inferences:** Add 5–15 seconds

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
| `MAX_DOCUMENT_SIZE_MB` | 10 | Max document size for base64 and file upload (MB) |
| `UPLOAD_PATH` | /app/data/uploads | Where to store uploaded files |
| `RESULTS_PATH` | /app/data/results | Where to store result files |

### Infrastructure Services

- **RabbitMQ:** `RABBITMQ_URL` (default: `amqp://guest:guest@rabbitmq:5672`) <!-- NOTE: Do not use guest:guest in production. See .env.example for guidance. -->
- **Redis:** `REDIS_URL` (default: `redis://redis:6379`)
- **Docling (Extraction):** `DOCLING_URL` (default: `http://localhost:8000`)
- **Regex Entity Extractor:** `REGEX_ENTITY_EXTRACTOR_URL` (default: `http://regex-entity-extractor:8081`)

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

### HTTP Status Codes and Error Responses

**All error responses follow this format:**
```json
{
  "error": "error_code",
  "detail": "Human-readable error message"
}
```

### Complete Error Reference

| HTTP Status | Error Code | Cause | Example |
|-------------|-----------|-------|---------|
| **400** | `invalid_request` | Malformed JSON or missing required fields | Missing `file` in multipart form |
| **400** | `invalid_input` | Validation failed (e.g., SSRF attempt, invalid JSON) | Suspicious URL in `document_url` |
| **400** | `invalid_file_type` | Uploaded file type not supported | Uploading `.exe` or `.zip` file |
| **400** | `file_too_large` | File exceeds size limit (see constraints table) | 15MB PDF when limit is 10MB |
| **400** | `csv_row_limit_exceeded` | CSV/Excel file exceeds row limit | 3,000 rows in spreadsheet when limit is 2,000 |
| **400** | `both_inputs_provided` | Both `document_base64` and `document_url` supplied | Mutually exclusive parameters |
| **400** | `missing_required_field` | Required field missing from request | `document_base64` and `document_url` both absent |
| **413** | `payload_too_large` | Request body exceeds maximum size | Uploading massive base64 encoded file |
| **429** | `rate_limit_exceeded` | Too many requests from this IP | More than 100 req/sec per IP |
| **404** | `not_found` | Job ID does not exist or has expired | Job TTL elapsed (24 hours default) |
| **425** | `too_early` | Job not yet completed | Attempting to download before job finishes |
| **500** | `internal_error` | Server-side error | Unexpected exception in handler |
| **500** | `extraction_error` | File extraction failed | Docling crashed on malformed PDF |
| **500** | `embedding_error` | Embedding generation failed | BAAI model unavailable or crashed |
| **500** | `entity_extraction_error` | Entity recognition failed | GLiNER service unreachable |
| **500** | `inference_error` | Inference generation failed (if `features: ["inferences"]` set) | vLLM service down |
| **500** | `storage_error` | Failed to save results | Disk full or permission denied |
| **503** | `service_unavailable` | Infrastructure services unavailable | Redis/RabbitMQ down |

### Error Response Examples

**400 Bad Request — Invalid file type:**
```json
{
  "error": "invalid_file_type",
  "detail": "File type '.exe' is not supported. Supported types: pdf, docx, txt, pptx, xls, xlsx, csv, json, jpg, jpeg, png, webp, bmp, mp3, wav, m4a, ogg, flac"
}
```

**413 Payload Too Large:**
```json
{
  "error": "file_too_large",
  "detail": "File size 15485760 bytes exceeds limit of 10485760 bytes (10 MB)"
}
```

**429 Rate Limit:**
```json
{
  "error": "rate_limit_exceeded",
  "detail": "Rate limit exceeded: 100 requests per second per IP"
}
```
**Response Headers:**
```
HTTP/1.1 429 Too Many Requests
Retry-After: 1
```

**404 Not Found:**
```json
{
  "error": "not_found",
  "detail": "Job 'job_invalid_id' does not exist or has expired (24h TTL)"
}
```

**500 Internal Error — Job failed at extraction:**
```json
{
  "error": "extraction_error",
  "detail": "Document extraction failed: PDF appears corrupted or uses unsupported compression"
}
```

### Handling Errors in Client Code

**Python example:**
```python
import requests

try:
    response = requests.post(
        "http://localhost:8080/v1/documents/upload",
        files={"file": open("document.pdf", "rb")}
    )
    response.raise_for_status()  # Raises HTTPError for 4xx/5xx
    job_id = response.json()["job_id"]
except requests.exceptions.HTTPError as e:
    error_data = e.response.json()
    print(f"Error: {error_data['error']}")
    print(f"Detail: {error_data['detail']}")
except Exception as e:
    print(f"Unexpected error: {e}")
```

**Bash example (with error handling):**
```bash
response=$(curl -s -w "\n%{http_code}" -X POST http://localhost:8080/v1/documents/upload \
  -F "file=@document.pdf")
http_code=$(echo "$response" | tail -n 1)
body=$(echo "$response" | head -n -1)

if [ "$http_code" = "202" ]; then
    echo "Job created: $(echo "$body" | jq -r '.job_id')"
else
    echo "Error ($http_code): $(echo "$body" | jq -r '.detail')"
    exit 1
fi
```

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

**Description:** Subscribe to real-time job status updates via Server-Sent Events (SSE). This endpoint streams status changes as they happen, allowing clients to monitor job progress in real-time without polling.

> **Note:** This endpoint uses the `/v1/jobs/` prefix, which differs from other endpoints that use `/v1/documents/`. Both `GET /v1/documents/{job_id}` (polling) and `GET /v1/jobs/{job_id}/stream` (SSE) are valid for monitoring job status.

**Path Parameters:**
- `job_id` (string, required): The job ID returned from job creation (e.g., `job_abc123def456`).

**Query Parameters:**
- None

**Response (200 OK):**
```
Content-Type: text/event-stream
Cache-Control: no-cache
Connection: keep-alive

event: job_pending
data: {"job_id":"job_abc123","status":"pending","timestamp":"2025-03-16T10:30:00Z"}

event: job_extracting
data: {"job_id":"job_abc123","status":"extracting","progress":0,"timestamp":"2025-03-16T10:30:05Z"}

event: job_embedding
data: {"job_id":"job_abc123","status":"embedding","progress":0.33,"timestamp":"2025-03-16T10:30:15Z"}

event: job_entities
data: {"job_id":"job_abc123","status":"entities","progress":0.66,"timestamp":"2025-03-16T10:30:25Z"}

event: job_completed
data: {"job_id":"job_abc123","status":"completed","progress":1.0,"timestamp":"2025-03-16T10:30:35Z","results_url":"http://localhost:8080/v1/documents/job_abc123/download"}

: heartbeat

```

**Event Types and Payloads:**

| Event | Status | Meaning | Payload |
|-------|--------|---------|---------|
| `job_pending` | `pending` | Job created, queued for processing | `{job_id, status, timestamp}` |
| `job_extracting` | `extracting` | Extracting content from file | `{job_id, status, progress, timestamp}` |
| `job_embedding` | `embedding` | Generating text embeddings | `{job_id, status, progress, timestamp}` |
| `job_entities` | `entities` | Extracting named entities | `{job_id, status, progress, timestamp}` |
| `job_inferences` | `inferences` | Generating inferences (if requested) | `{job_id, status, progress, timestamp}` |
| `job_metadata` | `metadata` | Extracting document metadata | `{job_id, status, progress, timestamp}` |
| `job_completed` | `completed` | Job finished successfully | `{job_id, status, progress: 1.0, timestamp, results_url}` |
| `job_failed` | `failed` | Job failed at some stage | `{job_id, status, error, error_detail, timestamp}` |

**Heartbeat:**
The server sends a comment-only message (`: heartbeat\n\n`) every **30 seconds** to keep the connection alive and prevent client timeouts. Clients should ignore heartbeat messages (they're not events).

**Connection Behavior:**
- **Timeout:** Connection closes after **10 minutes** of inactivity (maximum SSE stream duration)
- **Max Connections:** No hard limit; depends on server resources
- **Buffering:** Events are not buffered; if client disconnects, events during disconnection are lost (use polling for reliable delivery)

**Error Handling:**

If the job ID does not exist:
```
HTTP/1.1 404 Not Found

{
  "error": "not_found",
  "detail": "Job 'job_invalid_id' does not exist"
}
```

**Examples:**

**Basic streaming with curl (displays live events):**
```bash
curl -N http://localhost:8080/v1/jobs/job_abc123def456/stream
```

**Streaming with timeout (wait max 5 minutes):**
```bash
curl --max-time 300 -N http://localhost:8080/v1/jobs/job_abc123def456/stream
```

**Streaming in JavaScript/Node.js:**
```javascript
const eventSource = new EventSource('/v1/jobs/job_abc123def456/stream');

eventSource.addEventListener('job_pending', (event) => {
  const data = JSON.parse(event.data);
  console.log('Job pending:', data.job_id);
});

eventSource.addEventListener('job_extracting', (event) => {
  const data = JSON.parse(event.data);
  console.log('Extracting:', Math.round(data.progress * 100) + '%');
});

eventSource.addEventListener('job_completed', (event) => {
  const data = JSON.parse(event.data);
  console.log('Job completed! Download results:', data.results_url);
  eventSource.close();
});

eventSource.addEventListener('job_failed', (event) => {
  const data = JSON.parse(event.data);
  console.error('Job failed:', data.error);
  eventSource.close();
});

eventSource.onerror = () => {
  console.error('SSE connection error');
  eventSource.close();
};
```

**Python example using requests-sse:**
```python
import json
from sseclient import SSEClient

def stream_job(job_id):
    url = f'http://localhost:8080/v1/jobs/{job_id}/stream'
    client = SSEClient(url)
    
    try:
        for event in client:
            # Ignore heartbeat comments
            if event.data == ':heartbeat':
                continue
            
            data = json.loads(event.data)
            print(f"Event: {event.event}, Status: {data['status']}")
            
            if event.event == 'job_completed':
                print(f"Results ready at: {data['results_url']}")
                break
            elif event.event == 'job_failed':
                print(f"Job failed: {data['error']}")
                break
    except Exception as e:
        print(f"Connection lost: {e}")

stream_job('job_abc123def456')
```

**Polling vs. Streaming Comparison:**

| Feature | Polling (`GET /v1/documents/{job_id}`) | Streaming (`GET /v1/jobs/{job_id}/stream`) |
|---------|--------------------------------------|------------------------------------------|
| **Real-time updates** | No; requires polling interval | Yes; immediate notification |
| **Network overhead** | High (frequent requests) | Low (single connection) |
| **Event history** | Lost if missed polling window | Lost if client disconnects |
| **Max duration** | Unlimited | 10 minutes |
| **Best for** | Simple cases, slow jobs | Real-time dashboards, monitoring |

**Use polling for:** Long-running jobs where missing an intermediate status is acceptable.  
**Use streaming for:** Real-time progress dashboards, web UI job monitoring.

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
1. Check server logs: `docker logs textflow-orchestrator`
2. Review worker logs: `docker logs textflow-entities-worker`, etc.
3. Verify Redis/RabbitMQ are healthy: `make infra-status`
4. File an issue: GitHub Issues

