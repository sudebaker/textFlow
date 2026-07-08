# Deployment Guide

This guide covers the complete deployment of the IA Text Orchestrator to a single NVIDIA GPU machine, including air-gapped installation and upload/webhook document processing setup.

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Air-Gapped Deployment](#air-gapped-deployment)
   - [Overview](#overview)
   - [Quick Start](#quick-start)
   - [What Gets Packaged](#what-gets-packaged)
   - [Offline Mode](#offline-mode)
   - [Advanced: Incremental Updates](#advanced-incremental-updates)
   - [Troubleshooting](#troubleshooting)
   - [Rollback and Cleanup](#rollback-and-cleanup)
3. [Upload and Webhook Setup](#upload-and-webhook-setup)
   - [API Endpoints](#api-endpoints)
   - [Docker Volumes](#docker-volumes)
   - [File Flow](#file-flow)
   - [Webhook Payload](#webhook-payload)
4. [Verification](#verification)

---

## Prerequisites

### Build Machine

- Docker 20.10+ with Compose v2 (`docker compose`)
- NVIDIA Container Toolkit (to build GPU-enabled images)
- `rsync` installed
- `models/` directory populated with required model files (see [What Gets Packaged](#what-gets-packaged))
- ~50 GB free disk space for `dist/` output

### Target Machine

- Docker 20.10+ with Compose v2
- NVIDIA Container Toolkit and a compatible GPU driver
- ~40 GB free disk space
- `bash` 4.0+
- `curl` (used by `install.sh` health check)
- SSH access from the build machine

---

## Air-Gapped Deployment

This section covers deploying to a single NVIDIA GPU machine that has **no internet access at build time or runtime**. All model weights, Docker images, and configuration are self-contained in the `dist/` bundle.

### Overview

Deployment is a three-phase pipeline:

```
Build machine                     Transfer                  Target machine
─────────────────────            ──────────►              ─────────────────────
make package                      rsync ~43 GB             bash install.sh
  - builds Docker images                                     - loads images
  - saves images to .tar.gz                                  - extracts models
  - bundles models                                           - configures .env
  - writes dist/                                             - starts stack
                                                             - verifies health
```

### Quick Start

#### Step 1: Build and Package (on build machine)

Build Docker images and produce the deployment bundle:

```bash
make package
```

If the images are already built and you only need to repackage:

```bash
make package-skip-build
```

Expected `dist/` layout after packaging:

```
dist/
├── images/
│   ├── docker-orchestrator.tar.gz
│   ├── docker-embeddings-worker.tar.gz
│   ├── docker-entities-worker.tar.gz
│   ├── docker-extraction-worker.tar.gz
│   ├── docker-metadata-worker.tar.gz
│   ├── docker-completion-worker.tar.gz
│   ├── docker-resource-manager.tar.gz
│   ├── docker-regex-entity-extractor.tar.gz
│   ├── rabbitmq-3.12-management.tar.gz
│   ├── redis-7-alpine.tar.gz
│   └── docling-serve-latest.tar.gz
├── models.tar.gz
├── config/
│   ├── docker-compose.yml
│   ├── docker-compose.gpu.yml
│   └── .env.example
├── install.sh
└── MANIFEST.txt
```

#### Step 2: Transfer to Target

```bash
make deploy HOST=10.0.0.5
```

To use a specific SSH user:

```bash
make deploy HOST=user@10.0.0.5
```

This uses `rsync` to transfer `dist/` to `~/ia-text-deployment/` on the target. Estimated transfer time depends on network bandwidth; the bundle is approximately 43 GB.

#### Step 3: Install on Target

SSH into the target machine and run the installer:

```bash
ssh user@10.0.0.5
bash ~/ia-text-deployment/install.sh
```

You can also trigger this remotely from the build machine:

```bash
make install-remote HOST=user@10.0.0.5
```

`install.sh` performs these steps in order:

1. Checks prerequisites (Docker, nvidia-smi, disk space)
2. Loads all Docker images from `images/*.tar.gz`
3. Extracts `models.tar.gz`
4. Creates `.env` from `config/.env.example` and enforces air-gapped variables
5. Creates Docker named volumes (`redis-data`, `rabbitmq-data`) and bind-mount directories
6. Starts the stack: `docker compose -f config/docker-compose.yml -f config/docker-compose.gpu.yml up -d`
7. Polls `http://localhost:9080/health` for up to 60 seconds

Expected output at the end of a successful install:

```
======================================
  Deployment complete
======================================

  Service              URL
  -------------------- ----------------------------
  Orchestrator API     http://localhost:9080
  Orchestrator health  http://localhost:9080/health
  Docling              http://localhost:8000
  RabbitMQ management  http://localhost:15672
```

### What Gets Packaged

| Component | Approx. Size | Notes |
|-----------|-------------|-------|
| Docker images (11 total) | ~32 GB | 8 custom services + rabbitmq, redis, docling-serve |
| Model weights archive | ~11 GB | BGE-M3, GLiNER, DeBERTa-v3-small, Docling artifacts |
| Config files | < 1 MB | docker-compose.yml, docker-compose.gpu.yml, .env.example |
| **Total** | **~43 GB** | |

#### Docker Images

The Docker Compose project name is `docker` (because compose files live under `deploy/docker/`), so all custom images have the `docker-` prefix:

| Image | Role |
|-------|------|
| `docker-orchestrator` | REST API, job routing (port 8080) |
| `docker-embeddings-worker` | BAAI/bge-m3 vector embeddings |
| `docker-entities-worker` | GLiNER named-entity recognition |
| `docker-extraction-worker` | Document extraction via Unstructured API |
| `docker-metadata-worker` | Metadata extraction |
| `docker-completion-worker` | LLM completion |
| `docker-resource-manager` | GPU monitoring (port 9090) |
| `docker-regex-entity-extractor` | Regex-based entity extraction |
| `rabbitmq:3.12-management` | Message broker (ports 5672, 15672) |
| `redis:7-alpine` | Job state store (port 6379) |
| `quay.io/docling-project/docling-serve:latest` | Document conversion (port 8000) |

#### Model Files

The `models/` directory must contain:

```
models/
├── bge-m3/                  # embeddings-worker: BAAI/bge-m3
├── gliner-small-v2.1/       # entities-worker: GLiNER extractor
│   ├── gliner_config.json
│   └── pytorch_model.bin
├── deberta-v3-small/        # entities-worker: GLiNER backbone
│   ├── config.json
│   ├── pytorch_model.bin
│   ├── spm.model
│   └── tokenizer_config.json
├── modern-gliner/           # embeddings-worker: GLiNER variant
└── docling/                 # docling-serve: document conversion artifacts
```

### Offline Mode

`install.sh` enforces the following environment variables in `.env` regardless of whether `.env` already existed:

| Variable | Value | Effect |
|----------|-------|--------|
| `HF_HUB_OFFLINE` | `1` | Disables all Hugging Face Hub network calls |
| `TRANSFORMERS_OFFLINE` | `1` | Prevents `transformers` library from fetching weights |
| `ALLOW_REMOTE_DOWNLOAD` | `false` | Application-level guard against remote model downloads |

These variables are set via `sed -i` in-place replacement (or appended if absent), so re-running `install.sh` on an existing deployment will not overwrite other `.env` customizations.

### Advanced: Incremental Updates

To update a single service without retransferring the full bundle (including models):

**On the build machine**, rebuild and save only the changed image:

```bash
# Rebuild one service
docker compose -f deploy/docker/docker-compose.yml \
               -f deploy/docker/docker-compose.gpu.yml \
               build orchestrator

# Save it
docker save docker-orchestrator:latest | gzip > /tmp/docker-orchestrator.tar.gz

# Transfer only that image
rsync -avz --progress /tmp/docker-orchestrator.tar.gz \
      user@10.0.0.5:~/ia-text-deployment/images/
```

**On the target machine**, load and restart:

```bash
docker load -i ~/ia-text-deployment/images/docker-orchestrator.tar.gz

docker compose -f ~/ia-text-deployment/config/docker-compose.yml \
               -f ~/ia-text-deployment/config/docker-compose.gpu.yml \
               up -d --no-deps orchestrator
```

### Troubleshooting

#### Docker images won't load: permission denied

```
Got permission denied while trying to connect to the Docker daemon socket
```

The user running `install.sh` is not in the `docker` group:

```bash
sudo usermod -aG docker $USER
# Log out and back in, then retry
bash ~/ia-text-deployment/install.sh
```

#### Out of disk space

`install.sh` requires 40 GB free before it starts. If it fails at the disk check:

```bash
# Check current usage
df -h .

# Free space: remove old Docker images and build cache
docker system prune -a --volumes

# Retry
bash ~/ia-text-deployment/install.sh
```

#### Orchestrator not healthy after 60 seconds

`install.sh` warns (but does not fail) if the orchestrator is not healthy within 60 s. Investigate:

```bash
# Check orchestrator logs
docker compose -f ~/ia-text-deployment/config/docker-compose.yml \
               logs orchestrator --tail=50

# Check all container statuses
docker compose -f ~/ia-text-deployment/config/docker-compose.yml ps

# Verify GPU driver is visible inside containers
docker run --rm --gpus all nvidia/cuda:12.0-base nvidia-smi

# Check available VRAM (workers require several GB)
nvidia-smi

# Confirm models were extracted
ls ~/ia-text-deployment/models/
```

If models were not extracted (e.g., disk ran out mid-extraction):

```bash
rm -rf ~/ia-text-deployment/models
bash ~/ia-text-deployment/install.sh   # idempotent — safe to re-run
```

#### install.sh fails mid-way

`install.sh` is idempotent. Every step guards against repeating work it already completed:

- Images already loaded are skipped (Docker deduplicates layers).
- `models/` already extracted is skipped.
- Existing `.env` is kept; only the three air-gapped variables are updated.
- Docker volumes that already exist are not recreated.

Fix the underlying issue (disk space, permissions, missing file) and re-run:

```bash
bash ~/ia-text-deployment/install.sh
```

### Rollback and Cleanup

#### Stop the stack

```bash
docker compose -f ~/ia-text-deployment/config/docker-compose.yml down
```

#### Stop the stack and remove all data volumes

```bash
docker compose -f ~/ia-text-deployment/config/docker-compose.yml down -v
```

#### Remove named volumes manually

```bash
docker volume rm redis-data rabbitmq-data
```

#### Remove all loaded images

```bash
# Remove custom images
docker rmi \
  docker-orchestrator:latest \
  docker-embeddings-worker:latest \
  docker-entities-worker:latest \
  docker-extraction-worker:latest \
  docker-metadata-worker:latest \
  docker-completion-worker:latest \
  docker-resource-manager:latest \
  docker-regex-entity-extractor:latest

# Remove external images
docker rmi \
  rabbitmq:3.12-management \
  redis:7-alpine \
  quay.io/docling-project/docling-serve:latest
```

#### Remove model files

```bash
rm -rf ~/ia-text-deployment/models
```

#### Full cleanup

```bash
docker compose -f ~/ia-text-deployment/config/docker-compose.yml down -v
docker system prune -a --volumes
rm -rf ~/ia-text-deployment
```

---

## Upload and Webhook Setup

This section covers the upload + webhook document processing system. Clients upload a document via API, poll for status, and receive a webhook notification with a download URL upon completion.

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/v1/documents/upload` | POST | Upload file (multipart/form-data) |
| `/v1/documents/:id` | GET | Job status |
| `/v1/documents/:id/download` | GET | Download result JSON |
| `/v1/documents/:id` | DELETE | Delete job |

### Docker Volumes

Named Docker volumes for persisted upload and result storage:

```yaml
volumes:
  - uploads-data:/app/data/uploads
  - results-data:/app/data/results
```

### File Flow

```
Upload:   /app/data/uploads/{job_id}_{filename}.pdf
Result:   /app/data/results/{job_id}.json
```

### Implementation Checklist

| # | Task | File | Description |
|---|------|------|-------------|
| 1 | Config | `internal/config/config.go` | Add `WebhookURL`, `UploadPath`, `ResultsPath` fields |
| 2 | Models | `internal/models/job.go` | Add `DocumentPath`, `NotifyWebhook` to `JobMessage`; add `UploadRequest` for multipart |
| 3 | Orchestrator endpoints | `cmd/orchestrator/main.go` | POST `/v1/documents/upload` — save file, publish job; GET `/v1/documents/:id/download` — serve JSON |
| 4 | Extraction worker | `cmd/extraction-worker/worker.py` | Add `extract_text_from_file()` to read from `document_path` |
| 5 | Completion worker | `cmd/completion-worker/worker.py` | Add `save_results_to_file()` (save JSON to `/app/data/results/{job_id}.json`); add `send_webhook()` (POST to configured webhook URL) |
| 6 | Docker Compose | `deploy/docker/docker-compose.yml` | Add `uploads-data` and `results-data` volumes; add env vars `WEBHOOK_URL`, `RESULTS_PATH`, `API_BASE_URL`, `UPLOAD_PATH` |

### Webhook Payload

When a job completes or fails, the completion worker sends a POST request to `{WEBHOOK_URL}` with the following JSON body:

```json
{
  "job_id": "12345",
  "status": "completed|failed",
  "download_url": "http://api/v1/documents/12345/download",
  "error": "..."
}
```

---

## Verification

### Check all containers are running

```bash
docker compose -f ~/ia-text-deployment/config/docker-compose.yml ps
```

### Confirm the orchestrator is healthy

```bash
curl http://localhost:9080/health
```

### Submit a test document via upload

```bash
curl -X POST http://localhost:9080/v1/documents/upload \
  -F "file=@/path/to/test.pdf"
```

Expected response:

```json
{
  "job_id": "abc123",
  "status": "processing"
}
```

### Poll for job status

```bash
curl http://localhost:9080/v1/documents/abc123
```

### Download completed results

```bash
curl -O http://localhost:9080/v1/documents/abc123/download
```

### Submit a test document via URL

```bash
curl -X POST http://localhost:9080/api/v1/documents/process \
  -H "Content-Type: application/json" \
  -d '{"document_url": "http://example.com/test.pdf"}'
```

### Check RabbitMQ queues

Navigate to http://localhost:15672 (guest / guest by default).
