# Worker Common Library — Quick Reference

Shared utilities for the Python workers in `pkg/worker_common/`.

## Installation

```bash
cd pkg/worker_common && pip install -e .
```

## Configuration

`WorkerConfig` reads the standard environment variables and validates the
required ones:

```python
from pkg.worker_common.config import WorkerConfig, get_env, get_int_env, get_bool_env

config = WorkerConfig("embeddings")   # worker_name, not individual fields

config.rabbitmq_url      # RABBITMQ_URL (required)
config.redis_url         # REDIS_URL (required)
config.queue_name        # QUEUE_NAME (default: worker_name)
config.prefetch_count    # PREFETCH_COUNT (default: 5)
config.metrics_port      # METRICS_PORT (default: 8000)
config.log_level         # LOG_LEVEL (default: INFO)
config.max_retries       # MAX_RETRIES (default: 3)

port = get_int_env("METRICS_PORT", 8001)
debug = get_bool_env("DEBUG", False)
```

## RabbitMQ

```python
from pkg.worker_common.rabbitmq import connect_rabbitmq, declare_queue

with connect_rabbitmq(config.rabbitmq_url, prefetch_count=config.prefetch_count) as (conn, ch):
    declare_queue(ch, config.queue_name)
    ch.start_consuming()
```

`declare_queue` uses the shared argument table
(`pkg/worker_common/queue_args.py`), which mirrors
`internal/broker/rabbitmq.go:declareQueue()`.

For the full lifecycle (reconnection, DLQ, graceful shutdown) use
`BaseWorker` instead of wiring pika by hand:

```python
from pkg.worker_common.base import BaseWorker

class MyWorker(BaseWorker):
    def __init__(self):
        super().__init__(worker_name="my-worker", queue_name="my_queue")

    def process_message(self, message: dict):
        ...
```

## Metrics

```python
from pkg.worker_common.metrics import create_worker_metrics, create_gpu_metrics

metrics = create_worker_metrics("embeddings")
metrics["jobs_total"].labels(status="success").inc()
with metrics["job_duration"].time():
    process_job()
metrics["errors_total"].labels(error_type="timeout").inc()

gpu_available = create_gpu_metrics("embeddings")
gpu_available.labels(device="cuda:0").set(1)
```

## Graceful shutdown

```python
from pkg.worker_common.signals import SignalHandler

handler = SignalHandler()
handler.register(cleanup_callback=lambda: conn.close())

while not handler.should_exit:
    ...
```

## Resource manager

```python
from pkg.worker_common.resource_manager import ResourceManagerClient

client = ResourceManagerClient("http://localhost:9090")
resource = client.allocate_resource("gpu", worker_id="worker-1")
try:
    use(resource["device"])
finally:
    client.release_resource(resource["resource_id"])
```

## Artifact store

Large values are stored on disk and referenced from Redis:

```python
from pkg.worker_common.artifact_store import STORE, resolve, resolve_text

ref = STORE.put(b"...")
payload = resolve(STORE, ref)
text = resolve_text(STORE, ref)
```

## Environment variables

```bash
RABBITMQ_URL="amqp://user:pass@host:5672/"
REDIS_URL="redis://localhost:6379"
QUEUE_NAME="my_queue"
PREFETCH_COUNT=5
METRICS_PORT=8001
RESOURCE_MANAGER_URL="http://localhost:9090"
MAX_RETRIES=3
```

## More info

- Full package docs: `pkg/worker_common/README.md`
- Example worker: `pkg/worker_common/example_worker.py`
- Queue declaration parity: `pkg/worker_common/queue_args.py`
