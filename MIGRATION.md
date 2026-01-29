# Worker Migration Guide

This guide explains how to refactor existing Python workers to use the new `worker_common` library, which eliminates code duplication and provides standardized utilities.

## Table of Contents

1. [Overview](#overview)
2. [Installation](#installation)
3. [Migration Steps](#migration-steps)
4. [Before/After Examples](#beforeafter-examples)
5. [Testing](#testing)
6. [Rollback Plan](#rollback-plan)

---

## Overview

### What is `worker_common`?

`worker_common` is a shared Python library that provides:

- **RabbitMQ utilities**: Connection management, URL parsing
- **Resource Manager client**: GPU/CPU resource allocation
- **Prometheus metrics**: Standardized worker and GPU metrics
- **Signal handling**: Graceful shutdown logic
- **Configuration helpers**: Environment variable parsing with validation

### Benefits

- **Reduced code duplication**: ~200-300 lines removed per worker
- **Consistency**: All workers use same connection/error handling logic
- **Maintainability**: Bug fixes and improvements in one place
- **Type safety**: Proper type hints throughout
- **Testing**: Shared code is tested once

---

## Installation

### Step 1: Install the library

From the project root:

```bash
cd pkg/worker_common
pip install -e .
```

Or add to your worker's `requirements.txt`:

```txt
# Local package (editable install for development)
-e ../../pkg/worker_common

# OR for production (after publishing)
worker-common==1.0.0
```

### Step 2: Verify installation

```bash
python -c "from worker_common import WorkerConfig, SignalHandler; print('OK')"
```

---

## Migration Steps

### 1. Update Imports

#### Before:
```python
import os
import sys
import signal
import pika
from prometheus_client import Counter, Histogram, Gauge, start_http_server
```

#### After:
```python
from worker_common.config import WorkerConfig, get_env, get_int_env
from worker_common.rabbitmq import parse_rabbitmq_url, rabbitmq_connection
from worker_common.resource_manager import ResourceManagerClient
from worker_common.metrics import create_worker_metrics, create_gpu_metrics
from worker_common.signals import SignalHandler
```

---

### 2. Replace Configuration Parsing

#### Before:
```python
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/")
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
QUEUE_NAME = os.getenv("QUEUE_NAME", "embeddings_queue")
PREFETCH_COUNT = int(os.getenv("PREFETCH_COUNT", "5"))
RESOURCE_MANAGER_URL = os.getenv("RESOURCE_MANAGER_URL", "http://localhost:8081")
```

#### After:
```python
config = WorkerConfig(
    rabbitmq_url=get_env("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/"),
    redis_host=get_env("REDIS_HOST", "localhost"),
    redis_port=get_int_env("REDIS_PORT", 6379),
    queue_name=get_env("QUEUE_NAME", "embeddings_queue"),
    prefetch_count=get_int_env("PREFETCH_COUNT", 5),
    resource_manager_url=get_env("RESOURCE_MANAGER_URL", "http://localhost:8081"),
    worker_name="embeddings-worker",
    metrics_port=get_int_env("METRICS_PORT", 8001)
)
```

---

### 3. Replace RabbitMQ Connection

#### Before:
```python
def parse_rabbitmq_url(url):
    """Parse RabbitMQ URL into connection parameters."""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    return {
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 5672,
        "virtual_host": parsed.path[1:] if parsed.path else "/",
        "credentials": pika.PlainCredentials(
            parsed.username or "guest",
            parsed.password or "guest"
        )
    }

params = pika.ConnectionParameters(**parse_rabbitmq_url(RABBITMQ_URL))
connection = pika.BlockingConnection(params)
channel = connection.channel()
```

#### After:
```python
with rabbitmq_connection(config.rabbitmq_url) as (connection, channel):
    channel.basic_qos(prefetch_count=config.prefetch_count)
    # Your worker logic here
```

---

### 4. Replace Signal Handling

#### Before:
```python
shutdown_event = False

def signal_handler(signum, frame):
    global shutdown_event
    print(f"Received signal {signum}, shutting down gracefully...")
    shutdown_event = True

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# In main loop
while not shutdown_event:
    # Process messages
    pass
```

#### After:
```python
signal_handler = SignalHandler()

# In main loop
while not signal_handler.should_shutdown:
    # Process messages
    pass
```

---

### 5. Replace Metrics Setup

#### Before:
```python
MESSAGES_PROCESSED = Counter("worker_messages_processed_total", "Total messages", ["worker", "status"])
PROCESSING_TIME = Histogram("worker_processing_seconds", "Processing time", ["worker"])
GPU_MEMORY = Gauge("worker_gpu_memory_used_bytes", "GPU memory", ["worker", "device"])

start_http_server(8001)
```

#### After:
```python
metrics = create_worker_metrics(config.worker_name)
gpu_metrics = create_gpu_metrics(config.worker_name)

# Metrics server started automatically by WorkerConfig
# Or manually: config.start_metrics_server()

# Usage:
metrics['messages_processed'].labels(worker=config.worker_name, status="success").inc()
metrics['processing_time'].labels(worker=config.worker_name).observe(duration)
gpu_metrics['memory_used'].labels(worker=config.worker_name, device="cuda:0").set(memory_bytes)
```

---

### 6. Replace Resource Manager Client

#### Before:
```python
class ResourceManagerClient:
    def __init__(self, base_url):
        self.base_url = base_url

    def acquire_resource(self, resource_type, worker_id):
        response = requests.post(f"{self.base_url}/acquire", json={...})
        # ... error handling
        return response.json()

    def release_resource(self, resource_id):
        # ... implementation

resource_client = ResourceManagerClient(RESOURCE_MANAGER_URL)
```

#### After:
```python
resource_client = ResourceManagerClient(config.resource_manager_url)

# Same API, improved error handling
resource = resource_client.acquire_resource("gpu", worker_id="embeddings-001")
try:
    # Use resource
    pass
finally:
    resource_client.release_resource(resource["resource_id"])
```

---

## Before/After Examples

### Complete Worker Example

#### Before (embeddings-worker - ~350 lines):

```python
import os
import sys
import json
import signal
import logging
import pika
from prometheus_client import Counter, Histogram, start_http_server

# Duplicated code (50+ lines)
def parse_rabbitmq_url(url):
    # ... 15 lines

class ResourceManagerClient:
    # ... 40 lines

# Configuration (20+ lines)
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "...")
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
# ... 15 more env vars

# Metrics (15+ lines)
MESSAGES_PROCESSED = Counter(...)
PROCESSING_TIME = Histogram(...)
# ... 10 more metrics

# Signal handling (10+ lines)
shutdown_event = False
def signal_handler(signum, frame):
    # ...

# Main worker logic (200+ lines)
def main():
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    params = pika.ConnectionParameters(**parse_rabbitmq_url(RABBITMQ_URL))
    connection = pika.BlockingConnection(params)
    channel = connection.channel()
    
    while not shutdown_event:
        # ... worker logic
    
    connection.close()

if __name__ == "__main__":
    main()
```

#### After (embeddings-worker - ~150 lines):

```python
import json
import logging
from worker_common.config import WorkerConfig, get_env, get_int_env
from worker_common.rabbitmq import rabbitmq_connection
from worker_common.resource_manager import ResourceManagerClient
from worker_common.metrics import create_worker_metrics, create_gpu_metrics
from worker_common.signals import SignalHandler

# Configuration (10 lines)
config = WorkerConfig(
    rabbitmq_url=get_env("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/"),
    queue_name=get_env("QUEUE_NAME", "embeddings_queue"),
    prefetch_count=get_int_env("PREFETCH_COUNT", 5),
    worker_name="embeddings-worker",
    metrics_port=get_int_env("METRICS_PORT", 8001)
)

# Metrics (5 lines)
metrics = create_worker_metrics(config.worker_name)
gpu_metrics = create_gpu_metrics(config.worker_name)

# Main worker logic (120 lines)
def main():
    signal_handler = SignalHandler()
    resource_client = ResourceManagerClient(config.resource_manager_url)
    
    with rabbitmq_connection(config.rabbitmq_url) as (connection, channel):
        channel.basic_qos(prefetch_count=config.prefetch_count)
        
        while not signal_handler.should_shutdown:
            # ... worker logic (same as before)
    
    logging.info("Worker shut down gracefully")

if __name__ == "__main__":
    config.start_metrics_server()
    main()
```

**Code reduction: 200 lines (~57%)**

---

## Testing

### Unit Tests

The `worker_common` library includes unit tests. To run them:

```bash
cd pkg/worker_common
pytest tests/ -v --cov=worker_common
```

### Integration Tests

After migrating a worker, test it:

```bash
# Start dependencies
docker-compose -f deploy/docker/docker-compose.yml up -d rabbitmq redis

# Run migrated worker
cd cmd/embeddings-worker
python worker.py

# In another terminal, send test message
python -c "
import pika
connection = pika.BlockingConnection(pika.ConnectionParameters('localhost'))
channel = connection.channel()
channel.basic_publish(
    exchange='',
    routing_key='embeddings_queue',
    body='{\"job_id\": \"test-123\", \"text\": \"Hello world\"}'
)
print('Message sent')
connection.close()
"

# Check logs for successful processing
# Check metrics: curl http://localhost:8001/metrics
```

### Validation Checklist

- [ ] Worker starts without errors
- [ ] Connects to RabbitMQ successfully
- [ ] Processes messages correctly
- [ ] Metrics are exposed on configured port
- [ ] Graceful shutdown works (Ctrl+C)
- [ ] Resource acquisition/release works
- [ ] Error handling unchanged
- [ ] Performance is equivalent or better

---

## Rollback Plan

If issues occur after migration:

### Quick Rollback

```bash
# Revert to previous commit
git revert HEAD

# OR checkout previous version
git checkout <previous-commit-hash> cmd/embeddings-worker/worker.py

# Restart worker
docker-compose restart embeddings-worker
```

### Gradual Migration

Migrate one worker at a time:

1. **Week 1**: Migrate `metadata-worker` (simplest, no GPU)
2. **Week 2**: Monitor metrics, migrate `entities-worker`
3. **Week 3**: Monitor metrics, migrate `embeddings-worker`

### Canary Deployment

Run both old and new versions in parallel:

```yaml
# docker-compose.yml
embeddings-worker-old:
  image: embeddings-worker:v1.0
  environment:
    QUEUE_NAME: embeddings_queue
    
embeddings-worker-new:
  image: embeddings-worker:v2.0
  environment:
    QUEUE_NAME: embeddings_queue
```

Monitor error rates and performance for 24-48 hours before full cutover.

---

## Common Issues

### Import Error: `ModuleNotFoundError: No module named 'worker_common'`

**Solution:**
```bash
cd pkg/worker_common
pip install -e .
```

### Metrics Port Already in Use

**Solution:**
```bash
# Change port in environment
export METRICS_PORT=8002

# Or in WorkerConfig
config = WorkerConfig(..., metrics_port=8002)
```

### RabbitMQ Connection Timeout

**Solution:**
```python
# Add timeout to connection
from worker_common.rabbitmq import rabbitmq_connection

with rabbitmq_connection(url, timeout=30) as (conn, ch):
    # ...
```

### Signal Handler Not Working

**Solution:**
```python
# Ensure signal handler is created BEFORE main loop
signal_handler = SignalHandler()

# Check in loop
while not signal_handler.should_shutdown:
    # ...
```

---

## Migration Checklist

Use this checklist when migrating each worker:

- [ ] Install `worker_common` package
- [ ] Update imports
- [ ] Create `WorkerConfig` instance
- [ ] Replace RabbitMQ connection code
- [ ] Replace signal handling code
- [ ] Replace metrics initialization
- [ ] Replace Resource Manager client (if used)
- [ ] Remove duplicated utility functions
- [ ] Test locally
- [ ] Update worker Dockerfile (if needed)
- [ ] Update requirements.txt
- [ ] Run integration tests
- [ ] Deploy to staging
- [ ] Monitor for 24 hours
- [ ] Deploy to production
- [ ] Update documentation

---

## Support

If you encounter issues during migration:

1. Check this guide's [Common Issues](#common-issues) section
2. Review `pkg/worker_common/` source code
3. Check existing tests in `pkg/worker_common/tests/`
4. Open an issue in the project repository

---

## Next Steps

After completing migration:

1. **Remove old code**: Delete duplicated functions from workers
2. **Update documentation**: Update README.md with new structure
3. **Add worker tests**: Create pytest tests for worker logic
4. **Monitor metrics**: Compare before/after performance
5. **Iterate**: Add more utilities to `worker_common` as needed
