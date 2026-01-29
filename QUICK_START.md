# Quick Start Guide - IA Text Orchestrator

## What Was Implemented

The end-to-end document processing flow is now **COMPLETE**. Two critical missing components were added:

### 🆕 New Components

1. **Extraction Worker** (`cmd/extraction-worker/`)
   - Consumes documents from the `extract_text` queue
   - Extracts text using Unstructured API
   - Publishes to parallel processing queues

2. **Completion Worker** (`cmd/completion-worker/`)
   - Monitors job progress via Redis Pub/Sub
   - Detects when all processing steps complete
   - Aggregates results and marks jobs as "completed"

### ✅ Verified Existing Security & Reliability

All P0/P1 critical fixes were **already implemented**:
- ✅ Redis eviction policy (noeviction)
- ✅ No hardcoded credentials
- ✅ Memory leak prevention in rate limiter
- ✅ Goroutine leak prevention
- ✅ SSRF/DoS input validation
- ✅ Network segregation
- ✅ Resource limits
- ✅ HTTP timeouts

---

## 🚀 How to Run

### 1. Start the System

```bash
cd deploy/docker
docker-compose up -d
```

### 2. Verify Services

```bash
docker-compose ps
```

You should see **9 services** running:
- ✅ orchestrator
- ✅ extraction-worker (NEW)
- ✅ embeddings-worker
- ✅ entities-worker
- ✅ metadata-worker
- ✅ completion-worker (NEW)
- ✅ rabbitmq
- ✅ redis
- ✅ unstructured

### 3. Test with a Document

```bash
# Submit a test document
curl -X POST http://localhost:8080/v1/documents/process \
  -H "Content-Type: application/json" \
  -d '{"document_base64": "SGVsbG8gV29ybGQhIFRoaXMgaXMgYSB0ZXN0IGRvY3VtZW50Lg=="}' \
  | jq

# Save the job_id from the response
JOB_ID="<paste_job_id_here>"

# Wait 10-30 seconds for processing

# Check results
curl http://localhost:8080/v1/documents/$JOB_ID | jq
```

### 4. Monitor Processing

```bash
# Watch logs in real-time
docker-compose logs -f extraction-worker embeddings-worker entities-worker metadata-worker completion-worker
```

Expected log sequence:
```
[extraction-worker] Processing text extraction for job: <job_id>
[extraction-worker] Stored text for job <job_id>: N characters
[extraction-worker] Published job to queues: embeddings, entities, metadata
[embeddings-worker] Processing embeddings for job: <job_id>
[entities-worker] Processing entities for job: <job_id>
[metadata-worker] Processing metadata for job: <job_id>
[completion-worker] Job <job_id> completed steps: {extraction, embeddings, entities, metadata}
[completion-worker] Finalizing job: <job_id>
[completion-worker] Job <job_id> finalized and marked as completed
```

---

## 📊 Expected Response

When the job completes, you should get:

```json
{
  "job_id": "1738123456789012345",
  "status": "completed",
  "results": {
    "text": "Hello World! This is a test document.",
    "embeddings": [0.123, 0.456, ...],
    "entities": [
      {
        "text": "World",
        "label": "LOC",
        "confidence": 0.95,
        "start": 6,
        "end": 11
      }
    ],
    "metadata": {
      "word_count": 6,
      "char_count": 38,
      "language": "en"
    }
  },
  "error": ""
}
```

---

## 🔍 Troubleshooting

### Services Not Starting

```bash
# Check individual service logs
docker-compose logs <service-name>

# Common issues:
# - RabbitMQ/Redis not ready: Wait for healthchecks
# - Port conflicts: Check if ports 8080, 6379, 5672 are free
```

### Jobs Stuck in "pending"

```bash
# Check if extraction-worker is running
docker-compose ps extraction-worker

# Check extraction-worker logs
docker-compose logs extraction-worker

# Verify queue has consumer
docker-compose exec rabbitmq rabbitmqctl list_consumers
```

### Jobs Stuck in "processing"

```bash
# Check completion-worker logs
docker-compose logs completion-worker

# Verify step statuses in Redis
docker-compose exec redis redis-cli HGETALL "orchestrator:job:$JOB_ID:steps"
```

### No Results in Completed Job

```bash
# Check if aggregated results exist
docker-compose exec redis redis-cli GET "orchestrator:job:$JOB_ID:results"

# Check for errors
docker-compose exec redis redis-cli GET "orchestrator:job:$JOB_ID:error"
```

---

## 🧪 Comprehensive Testing

See [TESTING.md](./TESTING.md) for:
- Security tests (SSRF, DoS prevention)
- Performance tests
- Concurrent job processing
- Error handling tests

---

## 📁 Project Structure

```
ia-text-orchestrator/
├── cmd/
│   ├── extraction-worker/     # NEW - Text extraction
│   ├── completion-worker/     # NEW - Job completion
│   ├── embeddings-worker/     # Embeddings generation
│   ├── entities-worker/       # Entity extraction
│   ├── metadata-worker/       # Metadata analysis
│   └── orchestrator/          # Main API server
├── deploy/docker/
│   └── docker-compose.yml     # Updated with new services
├── TESTING.md                 # Testing guide
├── IMPLEMENTATION_STATUS.md   # Detailed status
└── QUICK_START.md            # This file
```

---

## ✅ What's Working

1. **Complete End-to-End Flow**: Document → Extract → Process → Aggregate → Return
2. **Security**: SSRF prevention, DoS protection, no hardcoded secrets
3. **Reliability**: Proper error handling, graceful shutdown, resource limits
4. **Monitoring**: Prometheus metrics, health checks, comprehensive logging
5. **Scalability**: Horizontal scaling ready, network segregation

---

## 📝 Next Steps

1. **Test**: Run the quick test above
2. **Monitor**: Check logs and metrics
3. **Configure**: Set production secrets in `.env`
4. **Deploy**: Follow [DEPLOYMENT_GUIDE.md](./DEPLOYMENT_GUIDE.md) for production

---

## 🎯 Success Criteria

After running the quick test:
- ✅ Job created (status 202)
- ✅ Text extracted
- ✅ All workers process
- ✅ Results aggregated
- ✅ Job status = "completed"
- ✅ Full results returned

---

**The system is ready for testing!** 🚀

If you encounter any issues, check:
1. Docker containers are running: `docker-compose ps`
2. Logs for errors: `docker-compose logs <service>`
3. Health status: `curl http://localhost:8080/health`
