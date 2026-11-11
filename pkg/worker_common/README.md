# Worker Common Library

Shared utilities for the Python workers in textFlow. The package removes
duplication across workers: configuration, RabbitMQ lifecycle, queue
declaration, metrics, signals, artifacts and pipeline routing.

## Installation

For local development:

```bash
pip install -e pkg/worker_common
```

In the container images the repository root is on `sys.path`, so workers
import it as `pkg.worker_common`.

## Recommended: `BaseWorker`

`BaseWorker` owns connection retries, queue declaration, acknowledgements,
dead-lettering and graceful shutdown. Subclasses only implement
`process_message`.

```python
from pkg.worker_common.base import BaseWorker

class MyWorker(BaseWorker):
    def __init__(self):
        super().__init__(worker_name="my-worker", queue_name="my_queue")

    def process_message(self, message: dict):
        # Do the work; raising sends the message to the DLQ flow.
        ...

if __name__ == "__main__":
    MyWorker().run()
```

Variants:

- `BaseAsyncWorker` (`async_base.py`) for aio-pika workers.
- `BasePubSubWorker` (`pubsub_base.py`) for Redis pub/sub consumers.

## `config.py`

```python
from pkg.worker_common.config import WorkerConfig, get_env, get_int_env, get_bool_env

config = WorkerConfig("embeddings")   # worker_name

config.rabbitmq_url     # RABBITMQ_URL (required)
config.redis_url        # REDIS_URL (required)
config.queue_name       # QUEUE_NAME (default: worker_name)
config.prefetch_count   # PREFETCH_COUNT (default: 5)
config.metrics_port     # METRICS_PORT (default: 8000)
config.log_level        # LOG_LEVEL (default: INFO)
config.max_retries      # MAX_RETRIES (default: 3)
```

## `rabbitmq.py`

```python
from pkg.worker_common.rabbitmq import connect_rabbitmq, declare_queue, parse_rabbitmq_url

with connect_rabbitmq("amqp://localhost", prefetch_count=5) as (connection, channel):
    declare_queue(channel, "my_queue")
    channel.basic_publish(exchange="", routing_key="my_queue", body=b"message")
```

`declare_queue()` uses `queue_args.build_queue_arguments()`, the single source
of truth shared with `internal/broker/rabbitmq.go:declareQueue()`. Any new
queue argument must be added there and in the Go declaration.

## `queue_args.py`

```python
from pkg.worker_common.queue_args import build_queue_arguments, DLX_EXCHANGE

arguments = build_queue_arguments("extract_text")
```

## `metrics.py`

```python
from pkg.worker_common.metrics import create_worker_metrics, create_gpu_metrics

metrics = create_worker_metrics("embeddings")
metrics["jobs_total"].labels(status="success").inc()
with metrics["job_duration"].time():
    process_job()
metrics["errors_total"].labels(error_type="timeout").inc()

create_gpu_metrics("embeddings").labels(device="cuda:0").set(1)
```

Metric names are derived from the worker name:
`{worker}_worker_jobs_total`, `{worker}_worker_job_duration_seconds`,
`{worker}_worker_errors_total`, `{worker}_worker_gpu_available`.

## `signals.py`

```python
from pkg.worker_common.signals import SignalHandler

handler = SignalHandler()
handler.register(cleanup_callback=lambda: connection.close())

while not handler.should_exit:
    ...
```

## `resource_manager.py`

```python
from pkg.worker_common.resource_manager import ResourceManagerClient

client = ResourceManagerClient("http://localhost:9090")
resource = client.allocate_resource("gpu", worker_id="worker-1")
try:
    use(resource["device"])
finally:
    client.release_resource(resource["resource_id"])
```

## `artifact_store.py`

Large payloads are written to disk and referenced from Redis as
`sha256:<hex>`; legacy raw values are still accepted.

```python
from pkg.worker_common.artifact_store import STORE, resolve, resolve_text

ref = STORE.put(b"...")
data = resolve(STORE, ref)
text = resolve_text(STORE, ref)
```

## `pipeline_config.py`

Loads `configs/pipeline.json` and answers the two routing questions:

```python
from pkg.worker_common.pipeline_config import PipelineDefinition

pipeline = PipelineDefinition.load()
queues = pipeline.queues_for(entry="extraction", features=["inferences"], profile="full")
steps = pipeline.steps_for(entry="extraction", features=[], profile="rag")
```

`queues_for` drives the entry-stage fan-out; `steps_for` drives the
completion-worker's required steps.

## Other modules

| Module | Purpose |
|---|---|
| `chunking.py` | Shared text chunking |
| `embeddings_client.py` | OpenAI-compatible `/v1/embeddings` client |
| `inference_dispatch.py` | Inference chunk dispatch and counters |
| `inference_embeddings.py` | Embeddings for inference results |
| `entity_utils.py` | Entity normalization and deduplication |
| `job_keys.py` | Canonical Redis key builders and TTLs |
| `security.py` | Upload path validation and signal helpers |
| `stage.py` | Stage result helpers |
| `artifact_gc.py` | Reachability-based artifact garbage collection |

## Tests

```bash
pytest pkg/worker_common/tests/ -v
```

Queue-declaration parity is anchored by
`pkg/worker_common/tests/test_queue_declaration_sites.py` and
`test_queue_args.py`.
