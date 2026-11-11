# textFlow — Deployment Guide

This guide covers packaging textFlow into a self-contained bundle and deploying
it to another machine, including air-gapped environments. GPU acceleration is
optional: the base Compose stack runs on CPU, and the GPU overlay is added when
the target has a compatible NVIDIA runtime.

## Prerequisites

### Build machine

- Docker 20.10+ with Compose v2 (`docker compose`)
- `rsync`
- `models/` populated for the models you operate (see [`MODELS.md`](MODELS.md))
- ~50 GB free disk space for `dist/`

### Target machine

- Docker 20.10+ with Compose v2
- `bash` 4.0+
- `curl`
- ~40 GB free disk space
- SSH access from the build machine
- Optional: NVIDIA driver + NVIDIA Container Toolkit for GPU acceleration

## Build and package

```bash
make package              # builds images and writes dist/
make package-skip-build   # repackages existing images
```

`dist/` contains:

```
dist/
├── images/*.tar.gz     # every image referenced by the Compose stack
├── models.tar.gz       # models staging tree
├── config/             # docker-compose.yml, docker-compose.gpu.yml, .env.example, scripts/
├── install.sh
└── MANIFEST.txt        # commit, timestamp, image digests, models checksum
```

The image list is derived from `deploy/docker/docker-compose.yml`, so it stays
in sync with the stack. It contains the custom services (orchestrator,
resource-manager, extraction/embeddings/entities/metadata/completion/inference/
audio/image workers, regex-entity-extractor, image-analyzer, vision-ocr,
whisper) plus the pinned third-party images (RabbitMQ, Redis, Docling,
Prometheus, Grafana, redis-exporter). `models.tar.gz` contains only file-based
models; embeddings, inference and vision models are resolved through the
operator-provided endpoints and are **not** bundled.

## Transfer and install

```bash
make deploy HOST=user@10.0.0.5
```

The bundle is copied to `~/textflow-deployment/`. Then either run the installer
remotely:

```bash
make install-remote HOST=user@10.0.0.5
```

or on the target:

```bash
cd ~/textflow-deployment
bash install.sh
```

`install.sh` is idempotent and performs:

1. Prerequisite checks (Docker, optional `nvidia-smi`, ≥40 GB free).
2. `docker load` of every `images/*.tar.gz`.
3. Extraction of `models.tar.gz` (skipped if `models/` already exists).
4. `.env` creation from `config/.env.example` (existing `.env` is preserved) and
   enforcement of `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`,
   `ALLOW_REMOTE_DOWNLOAD=false`, `MODELS_PATH=../models`.
5. `doctor.py` preflight on the environment and backends.
6. Creation of the named volumes and bind-mounted data directories.
7. Loading the staging tree into the shared `models-data` volume via the
   one-shot `models-sync` service.
8. `docker compose up -d` with the base file and the GPU overlay.
9. Health polling on `http://localhost:8080/health`.

Before starting, edit `~/textflow-deployment/.env` and fill the `BACKENDS`
block (`EMBEDDINGS_*`, `INFERENCE_*`). `doctor.py` fails fast if they are
missing or malformed.

## What runs where

| Service | Port | Notes |
|---|---|---|
| orchestrator | 8080 | REST API and `/metrics` |
| resource-manager | 9090 | GPU memory monitoring |
| docling | 5001→8000 | Document conversion |
| prometheus | 9091→9090 | Metrics |
| grafana | 3000 | Dashboards |
| RabbitMQ management | 15672 | Broker UI |

Workers have no published ports; they consume from RabbitMQ. The `image-analyzer`
and `whisper` services reach the operator's backends over `docker_default`; the
other worker networks are internal-only.

## GPU overlay

The base file is CPU-only. The installer always passes
`config/docker-compose.gpu.yml`; without an NVIDIA runtime those services fall
back to CPU. To run compose manually:

```bash
docker compose -f config/docker-compose.yml up -d
docker compose -f config/docker-compose.yml -f config/docker-compose.gpu.yml up -d
```

## Incremental updates

```bash
# Build and save one image on the build machine
docker compose -f deploy/docker/docker-compose.yml build orchestrator
docker save docker-orchestrator:latest | gzip > /tmp/orchestrator.tar.gz

# Transfer it
rsync /tmp/orchestrator.tar.gz user@10.0.0.5:~/textflow-deployment/images/

# On the target
docker load -i ~/textflow-deployment/images/docker-orchestrator.tar.gz
docker compose -f ~/textflow-deployment/config/docker-compose.yml \
  up -d --no-deps orchestrator
```

## Troubleshooting

- **Permission denied on the Docker socket.** Add the user to the `docker`
  group, log out and back in, then rerun `install.sh`.
- **Not enough disk space.** Free space (`docker system prune -a --volumes`) and
  rerun; the installer is safe to repeat.
- **Orchestrator not healthy after 60 s.** Check
  `docker compose -f config/docker-compose.yml logs orchestrator` and
  `docker compose -f config/docker-compose.yml ps`.
- **Models volume empty.** Rerun `install.sh`, or inspect it with
  `make models-verify`.
- **GPU not visible inside a container.** Verify `nvidia-smi` on the host and
  `docker info | grep -i runtime`; see [`GPU.md`](GPU.md).
- **`PRECONDITION_FAILED` on queues.** The queue arguments changed: drain and
  recreate the queues (or wipe the RabbitMQ volume in non-production setups)
  before starting the new version.

## Rollback and cleanup

```bash
docker compose -f ~/textflow-deployment/config/docker-compose.yml down
docker compose -f ~/textflow-deployment/config/docker-compose.yml down -v
docker system prune -a --volumes
rm -rf ~/textflow-deployment
```

Keep a previous `dist/` to roll back: it is self-contained and can be
reinstalled with the same procedure.

## Verification

```bash
curl http://localhost:8080/health
curl http://localhost:8080/metrics | head
docker compose -f ~/textflow-deployment/config/docker-compose.yml ps
```

Submit a document and follow its status:

```bash
curl -X POST http://localhost:8080/v1/documents/upload -F "file=@test.pdf"
curl http://localhost:8080/v1/documents/<job_id>
curl http://localhost:8080/v1/documents/<job_id>/download
```

`make smoke-test` runs an end-to-end check from the build machine against a
running stack. See [`API.md`](API.md) for the complete endpoint reference and
[`OPERATIONS.md`](OPERATIONS.md) for day-two operations.
