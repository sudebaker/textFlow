# Worker Common Library - Quick Reference

Fast reference for using `worker_common` in Python workers.

## Installation

```bash
cd pkg/worker_common && pip install -e .
```

## Basic Worker Template

```python
from worker_common.config import WorkerConfig, get_env, get_int_env
from worker_common.rabbitmq import rabbitmq_connection
from worker_common.metrics import create_worker_metrics
from worker_common.signals import SignalHandler

# 1. Configuration
config = WorkerConfig(
    rabbitmq_url=get_env("RABBITMQ_URL", "amqp://localhost:5672"),
    queue_name=get_env("QUEUE_NAME", "my_queue"),
    prefetch_count=get_int_env("PREFETCH_COUNT", 5),
    worker_name="my-worker",
    metrics_port=8001
)

# 2. Setup
signal_handler = SignalHandler()
metrics = create_worker_metrics(config.worker_name)
config.start_metrics_server()

# 3. Main loop
with rabbitmq_connection(config.rabbitmq_url) as (connection, channel):
    channel.basic_qos(prefetch_count=config.prefetch_count)
    channel.queue_declare(queue=config.queue_name, durable=True)
    
    for method, properties, body in channel.consume(config.queue_name):
        if signal_handler.should_shutdown:
            break
        
        # Process message
        try:
            result = process(body)
            metrics['messages_processed'].labels(
                worker=config.worker_name, status="success"
            ).inc()
            channel.basic_ack(method.delivery_tag)
        except Exception as e:
            metrics['messages_processed'].labels(
                worker=config.worker_name, status="error"
            ).inc()
            channel.basic_nack(method.delivery_tag, requeue=True)
```

## Common Patterns

### Configuration Loading
```python
from worker_common.config import get_env, get_int_env, get_bool_env

# String
redis_host = get_env("REDIS_HOST", "localhost")

# Integer
redis_port = get_int_env("REDIS_PORT", 6379)

# Boolean
use_gpu = get_bool_env("USE_GPU", False)
```

### Resource Manager (GPU/CPU)
```python
from worker_common.resource_manager import ResourceManagerClient

client = ResourceManagerClient("http://localhost:8081")

resource = client.acquire_resource("gpu", worker_id="worker-1")
try:
    # Use resource["device"] - e.g., "cuda:0"
    pass
finally:
    client.release_resource(resource["resource_id"])
```

### Metrics
```python
from worker_common.metrics import create_worker_metrics, create_gpu_metrics

# Standard metrics
metrics = create_worker_metrics("my-worker")
metrics['messages_processed'].labels(worker="my-worker", status="success").inc()
metrics['processing_time'].labels(worker="my-worker").observe(1.5)
metrics['queue_size'].labels(worker="my-worker", queue="tasks").set(10)

# GPU metrics
gpu_metrics = create_gpu_metrics("my-worker")
gpu_metrics['memory_used'].labels(worker="my-worker", device="cuda:0").set(8192)
gpu_metrics['utilization'].labels(worker="my-worker", device="cuda:0").set(85.5)
```

### Graceful Shutdown
```python
from worker_common.signals import SignalHandler

handler = SignalHandler()

while not handler.should_shutdown:
    # Work
    pass

print("Shutting down...")
```

## Environment Variables

```bash
# RabbitMQ
RABBITMQ_URL="amqp://user:pass@host:5672/vhost"
QUEUE_NAME="my_queue"
PREFETCH_COUNT=5

# Redis
REDIS_HOST="localhost"
REDIS_PORT=6379

# Resource Manager
RESOURCE_MANAGER_URL="http://localhost:8081"
REQUIRES_GPU=true

# Metrics
METRICS_PORT=8001
WORKER_NAME="my-worker"
```

## Troubleshooting

### ModuleNotFoundError
```bash
cd pkg/worker_common && pip install -e .
```

### Port in use
```python
config = WorkerConfig(..., metrics_port=8002)
```

### Connection timeout
```python
with rabbitmq_connection(url, timeout=60) as (conn, ch):
    pass
```

## More Info

- Full docs: `pkg/worker_common/README.md`
- Migration guide: `MIGRATION.md`
- Example: `pkg/worker_common/example_worker.py`
