# textFlow - Docker Deployment

This directory contains Docker Compose configuration for the textFlow, designed for **air-gapped (on-premise) deployment** with zero internet access.

## 🔒 Air-Gapped Deployment (CRITICAL)

This system is **NOT** designed for cloud deployment. All model files must be pre-downloaded and mounted as volumes.

### ✅ What's Included
- Docker Compose orchestration for 11 microservices
- RabbitMQ message queue
- Redis cache
- Docling document extraction service
- GLiNER entity extraction (offline)
- BGE-M3 embeddings (offline)
- Regex entity extractor

### ❌ What's NOT Included
- Internet access (not needed, not allowed)
- HuggingFace model downloads (blocked by design)
- Model files (must be pre-downloaded to `./models/`)

## 📋 Prerequisites

### 1. Download Model Files

Before building, ensure all models are downloaded to the host machine:

```bash
# Create models directory
mkdir -p models/

# Required models:
models/
├── bge-m3/                    # Embeddings model (BAAI/bge-m3)
├── deberta-v3-small/         # Tokenizer backbone for GLiNER
├── gliner-small-v2.1/        # Entity extraction model
└── modern-gliner/            # Alternative GLiNER variant
```

**Model file requirements:**

| Model | Files Required | Size |
|-------|----------------|------|
| `bge-m3/` | 10 files (.bin, .json, .txt) | ~1GB |
| `deberta-v3-small/` | 8 files (.bin, .json, .model) | ~300MB |
| `gliner-small-v2.1/` | 8 files (.bin, .json, config) | ~800MB |
| `modern-gliner/` | 13 files | ~1.5GB |

**Total: ~3.6GB of model files required**

### 2. Create Environment File

Copy the example environment file:

```bash
cp ../../.env.example .env
```

Configure for your deployment:

```env
# RabbitMQ
# NOTE: Do not use guest:guest in production. See .env.example for guidance.
RABBITMQ_USER=guest
RABBITMQ_PASS=guest

# Device selection (leave blank for auto-detect)
EMBEDDINGS_DEVICE=cpu
ENTITIES_DEVICE=cpu

# Optional: Webhook notifications
WEBHOOK_URL=https://your-domain.com/webhooks/jobs
```

**Important**: Do NOT change these (air-gapped requirements):
```env
HF_HUB_OFFLINE=1
TRANSFORMERS_OFFLINE=1
ALLOW_REMOTE_DOWNLOAD=false
```

## 🚀 Quick Start

### 1. Build Images

```bash
docker compose build
```

### 2. Start Services

```bash
docker compose up -d
```

### 3. Verify Health

```bash
docker compose ps
```

All services should show **Up** or **Healthy** status. Wait ~30 seconds for initialization.

### 4. Test Orchestrator

```bash
curl http://localhost:9080/health
# Expected response: {"status":"healthy"}
```

## 📝 Configuration

### Environment Variables

All configuration is controlled via `.env` file:

```env
# Message Queue
RABBITMQ_URL=amqp://rabbitmq:5672/
REDIS_URL=redis://redis:6379

# Document Extraction
UNSTRUCTURED_URL=http://docling:8000
DOCLING_DEVICE=auto

# Entity Extraction
ENTITY_TYPES=PERSON,ORGANIZATION,LOCATION,DATE,MONEY,EMAIL
ENTITY_THRESHOLD_PERSON=0.30
ENTITY_THRESHOLD_DATE=0.45
ENTITY_THRESHOLD_MONEY=0.55

# Orchestrator
HTTP_PORT=8080
LOG_LEVEL=info
JOB_TTL=24h
MAX_RETRIES=3

# Worker Settings
PREFETCH_COUNT=5
ENTITIES_DEVICE=cpu
EMBEDDINGS_DEVICE=cpu
```

### Service Ports

| Service | Port (Host) | Port (Container) |
|---------|-------------|------------------|
| Orchestrator | 9080 | 8080 |
| Regex Entity Extractor | 8081 | 8081 |
| Docling | 8000 | 5001 |
| RabbitMQ Management | 15672 | 15672 |

## 📡 API Endpoints

Once running, the orchestrator is available at `http://localhost:9080`:

### Upload & Process Document
```bash
curl -X POST http://localhost:9080/v1/documents/upload \
  -F "file=@document.pdf"
```

### Get Job Status
```bash
curl http://localhost:9080/v1/documents/{job_id}
```

### Get Results
```bash
curl http://localhost:9080/v1/documents/{job_id} \
  | jq '.results'
```

## 🔍 Monitoring

### View Logs

```bash
# All services
docker compose logs -f

# Specific service
docker compose logs -f entities-worker
docker compose logs -f extraction-worker
docker compose logs -f embeddings-worker
```

### Metrics

Prometheus metrics available at:
- Orchestrator: `http://localhost:9080/metrics`
- Embeddings: `http://localhost:8001/metrics`
- Entities: `http://localhost:8002/metrics`

## 🛑 Troubleshooting

### Service Won't Start

Check logs:
```bash
docker compose logs service-name
```

Common issues:
- Missing model files → Download to `./models/`
- Port already in use → Change ports in `.env`
- Out of memory → Increase Docker memory limits

### Entities Not Extracted

Verify models are loaded:
```bash
docker compose logs entities-worker | grep -i "loaded"
```

### No Internet Access (Expected!)

If you see network errors:
- ✅ Expected: "Connection refused" to HuggingFace Hub
- ✅ Expected: "offline mode" messages in logs
- ❌ Wrong: Successful connection to huggingface.co

### Jobs Stuck in Processing

Check if workers are consuming messages:
```bash
docker compose logs extraction-worker
docker compose logs entities-worker
docker compose logs embeddings-worker
```

## 🧹 Cleanup

### Stop Services
```bash
docker compose down
```

### Remove Volumes (⚠️ DELETES DATA)
```bash
docker compose down -v
```

### Remove Containers
```bash
docker compose rm
```

## 🔐 Security Notes

### Air-Gapped Compliance
- ✅ No internet downloads at build time
- ✅ No internet downloads at runtime
- ✅ All models loaded from local `/models/` directory
- ✅ Environment variables block HuggingFace Hub access

### Network Isolation
For maximum security, isolate the deployment:
```bash
# Test offline (no network)
docker run --network=none docker-entities-worker /entrypoint.sh
```

### Credentials
- `.env` file is excluded from git (contains secrets)
- Only commit `.env.example`
- Never share `.env` file

## 📖 Additional Resources

- API Documentation: `../../docs/API.md`
- Configuration Guide: `../../AGENTS.md`
- Environment Variables: `../../.env.example`

## 🆘 Support

For issues:
1. Check logs: `docker compose logs -f`
2. Verify models exist: `ls -la models/`
3. Check `.env` configuration
4. Review AGENTS.md for known issues
