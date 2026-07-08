# Worker Common Library

Shared utilities for Python workers in the textFlow project.

## Core Classes

| Class | Module | Worker Type |
|-------|--------|-------------|
| `BaseWorker` | `base.py` | Sync RabbitMQ (pika) — entities, embeddings, inference, metadata |
| `BaseAsyncWorker` | `async_base.py` | Async RabbitMQ (aio_pika) — audio, image |
| `BasePubSubWorker` | `pubsub_base.py` | Redis pub/sub — completion |

## Modules

### `base.py`

`BaseWorker` class for sync pika-based workers (entities, embeddings, inference, metadata).

### `async_base.py`

`BaseAsyncWorker` class for aio_pika-based async workers (audio, image).

### `pubsub_base.py`

`BasePubSubWorker` class for Redis pub/sub workers (completion).

### `rabbitmq.py`

RabbitMQ connection utilities.

```python
from pkg.worker_common.rabbitmq import parse_rabbitmq_url, rabbitmq_connection

params = parse_rabbitmq_url("amqp://user:pass@host:5672/vhost")

with rabbitmq_connection("amqp://localhost") as (connection, channel):
    channel.basic_publish(exchange="", routing_key="queue", body="message")
```

**Functions:**
- `parse_rabbitmq_url(url)`: Parse AMQP URL into connection parameters
- `rabbitmq_connection(url, timeout=30)`: Context manager for connections
- `declare_queue_async(channel, queue_name)`: Declare a durable queue with DLX (async)

### `chunking.py`

Shared character-based text chunking for audio/image workers.

### `security.py`

Upload path validation to prevent path traversal.

## Migration Guide

See `BaseAsyncWorker` and `BasePubSubWorker` reference implementations in:
- `cmd/audio-worker/worker.py`
- `cmd/image-worker/worker.py`
- `cmd/completion-worker/worker.py`

## Development

### Dependencies

Core dependencies:
- `aio_pika>=9.3.0` - Async RabbitMQ client (for async workers)
- `pika>=1.3.0` - Sync RabbitMQ client (for legacy workers)
- `prometheus-client>=0.16.0` - Metrics
- `fastapi>=0.104.0` - Health check endpoints
- `uvicorn>=0.24.0` - ASGI server
- `redis>=5.0.0` - Redis client
- `pydantic-settings>=2.0.0` - Settings management

## Phase 4.1 Completion

**Status:** ✅ COMPLETED — Code deduplication for Python workers.

### What Was Delivered
- Shared base classes (`BaseWorker`, `BaseAsyncWorker`, `BasePubSubWorker`) with Redis, health server, Prometheus metrics, signal handling, Event Bus
- RabbitMQ connection management (`rabbitmq.py`, `rabbitmq_async.py`)
- Shared text chunking and upload path validation (`chunking.py`, `security.py`)
- Comprehensive `MIGRATION.md` with before/after comparisons (57% code reduction)

### Integration Pattern
```python
from worker_common.config import WorkerConfig
from worker_common.rabbitmq import rabbitmq_connection
from worker_common.metrics import create_worker_metrics
from worker_common.signals import SignalHandler

config = WorkerConfig(...)
signal_handler = SignalHandler()
metrics = create_worker_metrics(config.worker_name)

with rabbitmq_connection(config.rabbitmq_url) as (conn, ch):
    while not signal_handler.should_shutdown:
        # Worker-specific logic only
```

### Next Steps
1. Migrate workers one at a time (metadata → entities → embeddings)
2. Phase 4.2: JSON encoding pool, Redis pipelining, connection pooling
3. Create Python tests for base classes
4. Version and publish to private PyPI (optional)

## License

Same as parent project.
