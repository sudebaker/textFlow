# Embeddings Worker

RabbitMQ consumer that chunks text and generates embeddings through an
OpenAI-compatible backend (`POST /v1/embeddings`). It does **not** load a local
model and has no local fallback: the operator supplies the backend.

## Role

1. Consume a job message from the `embeddings` queue.
2. Resolve the job text from the artifact store.
3. Chunk the text.
4. Request embeddings from `EMBEDDINGS_BASE_URL` in batches.
5. Store the vectors and continue the pipeline.

The deployed entrypoint is `embeddings_worker.py` (see the Dockerfile
`CMD`). The standalone FastAPI app under `app/` is legacy and is not started by
the container.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `EMBEDDINGS_BASE_URL` | — (required) | Backend base URL, **without** `/v1`; the client appends `/v1/embeddings` |
| `EMBEDDINGS_MODEL` | `BAAI/bge-m3` | Model name sent to the backend |
| `EMBEDDINGS_DIMENSION` | `1024` | Expected vector dimension; validates responses |
| `EMBEDDINGS_TIMEOUT` | `30` | HTTP timeout in seconds |
| `EMBEDDINGS_MAX_RETRIES` | `3` | Retries with backoff |
| `EMBEDDINGS_BATCH_SIZE` | `32` | Texts per request |
| `EMBEDDINGS_CACHE_TTL_SECONDS` | `604800` | Cache TTL for `emb:{sha256(model+text)}` |
| `QUEUE_NAME` | `embeddings` | RabbitMQ queue |
| `METRICS_PORT` | `8001` | Prometheus metrics port |
| `EMBEDDING_BATCH_SIZE_GPU` / `_CPU` | `64` / `2` | Local batching hints |

The base URL must not include `/v1`; a doubled prefix results in silent
failures. In Docker, `host.docker.internal` reaches a backend running on the
host.

## Metrics

- `embeddings_worker_batch_duration_seconds{batch_size}`
- `embeddings_worker_chunks_total{status}`
- Base worker job metrics exposed by `pkg/worker_common`

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r cmd/embeddings-worker/requirements.txt

export EMBEDDINGS_BASE_URL=http://localhost:11434
export EMBEDDINGS_MODEL=nomic-embed-text
export EMBEDDINGS_DIMENSION=768

python cmd/embeddings-worker/embeddings_worker.py
```

Tests:

```bash
pytest cmd/embeddings-worker/tests/ -v
pytest pkg/worker_common/tests/test_embeddings_client.py -v
```

See [`docs/CONFIGURATION.md`](../../docs/CONFIGURATION.md) for the full
environment reference and [`docs/ARCHITECTURE.md`](../../docs/ARCHITECTURE.md)
for the pipeline.
