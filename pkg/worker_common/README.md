# Worker Common Library

Shared utilities for Python workers in the textFlow project.

## Overview

This package eliminates code duplication across Python workers by providing standardized utilities for:

- **Configuration Management**: Environment variable parsing with validation
- **RabbitMQ Integration**: Connection management and URL parsing
- **Resource Manager Client**: GPU/CPU resource allocation
- **Prometheus Metrics**: Standardized worker and GPU metrics
- **Signal Handling**: Graceful shutdown on SIGINT/SIGTERM

## Installation

### Development

From the project root:

```bash
cd pkg/worker_common
pip install -e .
```

### Production

```bash
pip install worker-common==1.0.0
```

Or add to `requirements.txt`:

```txt
worker-common==1.0.0
```

## Quick Start

```python
from worker_common.config import WorkerConfig, get_env, get_int_env
from worker_common.rabbitmq import rabbitmq_connection
from worker_common.metrics import create_worker_metrics
from worker_common.signals import SignalHandler

# Configure worker
config = WorkerConfig(
    rabbitmq_url=get_env("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/"),
    queue_name=get_env("QUEUE_NAME", "my_queue"),
    prefetch_count=get_int_env("PREFETCH_COUNT", 5),
    worker_name="my-worker",
    metrics_port=8001
)

# Setup
signal_handler = SignalHandler()
metrics = create_worker_metrics(config.worker_name)
config.start_metrics_server()

# Process messages
with rabbitmq_connection(config.rabbitmq_url) as (connection, channel):
    channel.basic_qos(prefetch_count=config.prefetch_count)
    
    while not signal_handler.should_shutdown:
        # Your worker logic here
        pass
```

## Modules

### `config.py`

Configuration management with type-safe environment variable parsing.

```python
from worker_common.config import WorkerConfig, get_env, get_int_env, get_bool_env

# Parse environment variables
redis_host = get_env("REDIS_HOST", "localhost")
redis_port = get_int_env("REDIS_PORT", 6379)
use_gpu = get_bool_env("USE_GPU", False)

# Create worker configuration
config = WorkerConfig(
    rabbitmq_url="amqp://localhost",
    queue_name="my_queue",
    worker_name="my-worker"
)

# Start metrics server
config.start_metrics_server()
```

**Classes:**
- `WorkerConfig`: Worker configuration container

**Functions:**
- `get_env(key, default)`: Get string from environment
- `get_int_env(key, default)`: Get integer from environment
- `get_bool_env(key, default)`: Get boolean from environment

### `rabbitmq.py`

RabbitMQ connection utilities.

```python
from worker_common.rabbitmq import parse_rabbitmq_url, rabbitmq_connection

# Parse URL
params = parse_rabbitmq_url("amqp://user:pass@host:5672/vhost")
# Returns: {"host": "host", "port": 5672, "virtual_host": "vhost", "credentials": ...}

# Connection context manager
with rabbitmq_connection("amqp://localhost") as (connection, channel):
    channel.basic_publish(exchange="", routing_key="queue", body="message")
```

**Functions:**
- `parse_rabbitmq_url(url)`: Parse AMQP URL into connection parameters
- `rabbitmq_connection(url, timeout=30)`: Context manager for connections

### `resource_manager.py`

Resource Manager client for GPU/CPU allocation.

```python
from worker_common.resource_manager import ResourceManagerClient

client = ResourceManagerClient("http://localhost:8081")

# Acquire resource
resource = client.acquire_resource(resource_type="gpu", worker_id="worker-1")
# Returns: {"resource_id": "gpu-0", "device": "cuda:0", ...}

try:
    # Use resource
    pass
finally:
    # Release resource
    client.release_resource(resource["resource_id"])
```

**Classes:**
- `ResourceManagerClient`: HTTP client for resource management

**Methods:**
- `acquire_resource(resource_type, worker_id)`: Acquire GPU/CPU
- `release_resource(resource_id)`: Release resource
- `health_check()`: Check service health

### `metrics.py`

Prometheus metrics helpers.

```python
from worker_common.metrics import create_worker_metrics, create_gpu_metrics

# Create standard worker metrics
metrics = create_worker_metrics("my-worker")
metrics['messages_processed'].labels(worker="my-worker", status="success").inc()
metrics['processing_time'].labels(worker="my-worker").observe(1.5)
metrics['queue_size'].labels(worker="my-worker", queue="my_queue").set(10)

# Create GPU metrics
gpu_metrics = create_gpu_metrics("my-worker")
gpu_metrics['memory_used'].labels(worker="my-worker", device="cuda:0").set(8192)
gpu_metrics['utilization'].labels(worker="my-worker", device="cuda:0").set(85.5)
```

**Functions:**
- `create_worker_metrics(worker_name)`: Create standard worker metrics dict
- `create_gpu_metrics(worker_name)`: Create GPU-specific metrics dict

**Standard Metrics:**
- `messages_processed_total`: Counter (labels: worker, status)
- `processing_time_seconds`: Histogram (labels: worker)
- `queue_size`: Gauge (labels: worker, queue)
- `active_tasks`: Gauge (labels: worker)
- `errors_total`: Counter (labels: worker, error_type)

**GPU Metrics:**
- `gpu_memory_used_bytes`: Gauge (labels: worker, device)
- `gpu_utilization_percent`: Gauge (labels: worker, device)
- `gpu_temperature_celsius`: Gauge (labels: worker, device)

### `signals.py`

Graceful shutdown signal handling.

```python
from worker_common.signals import SignalHandler

handler = SignalHandler()

while not handler.should_shutdown:
    # Process work
    if handler.should_shutdown:
        break

print("Shutting down gracefully...")
```

**Classes:**
- `SignalHandler`: Handle SIGINT/SIGTERM signals

**Properties:**
- `should_shutdown`: Boolean indicating shutdown requested

## Examples

### Complete Worker Example

See `example_worker.py` for a complete working example demonstrating all features.

### Minimal Worker

```python
from worker_common.config import WorkerConfig
from worker_common.rabbitmq import rabbitmq_connection
from worker_common.signals import SignalHandler

config = WorkerConfig(
    rabbitmq_url="amqp://localhost",
    queue_name="tasks",
    worker_name="minimal-worker"
)

signal_handler = SignalHandler()

with rabbitmq_connection(config.rabbitmq_url) as (conn, ch):
    ch.queue_declare(queue=config.queue_name)
    
    for method, properties, body in ch.consume(config.queue_name):
        if signal_handler.should_shutdown:
            break
        
        # Process message
        print(f"Received: {body}")
        ch.basic_ack(method.delivery_tag)
```

## Migration Guide

See `../../MIGRATION.md` for detailed instructions on migrating existing workers to use this library.

### Quick Migration Checklist

- [ ] Install `worker_common` package
- [ ] Replace configuration parsing with `WorkerConfig`
- [ ] Replace RabbitMQ connection code with `rabbitmq_connection`
- [ ] Replace signal handling with `SignalHandler`
- [ ] Replace metrics setup with `create_worker_metrics`
- [ ] Remove duplicated utility functions
- [ ] Test locally
- [ ] Deploy and monitor

## Development

### Running Tests

```bash
cd pkg/worker_common
pip install -e ".[dev]"
pytest tests/ -v --cov=worker_common
```

### Code Style

```bash
# Format code
black .

# Lint
flake8 .
```

### Dependencies

Core dependencies:
- `pika>=1.3.0` - RabbitMQ client
- `prometheus-client>=0.16.0` - Metrics
- `requests>=2.28.0` - HTTP client

Development dependencies:
- `pytest>=7.0.0`
- `pytest-cov>=4.0.0`
- `black>=23.0.0`
- `flake8>=6.0.0`

## Benefits

Using `worker_common` provides:

- **Reduced Duplication**: ~200 lines removed per worker
- **Consistency**: All workers use same patterns
- **Type Safety**: Full type hints throughout
- **Testing**: Shared code tested once
- **Maintainability**: Bug fixes in one place
- **Standardization**: Common metrics and error handling

## Troubleshooting

### ModuleNotFoundError

```bash
cd pkg/worker_common
pip install -e .
```

### Metrics Port Already in Use

Change the port in configuration:

```python
config = WorkerConfig(..., metrics_port=8002)
```

### Connection Timeout

Increase timeout:

```python
with rabbitmq_connection(url, timeout=60) as (conn, ch):
    pass
```

## Support

- Check `MIGRATION.md` for migration help
- Review `example_worker.py` for usage examples
- Check source code for detailed documentation

## License

Same as parent project.

## Version History

### 1.0.0 (Current)

- Initial release
- Configuration management
- RabbitMQ utilities
- Resource Manager client
- Prometheus metrics
- Signal handling
