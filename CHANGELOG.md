# Changelog

Todos los cambios notables de textFlow se documentan aquí.

El formato está basado en [Keep a Changelog](https://keepachangelog.com/es/1.1.0/),
y este proyecto adhiere a [Semantic Versioning](https://semver.org/lang/es/).

## [Unreleased]

### Added
- API key authentication middleware para orchestrator (`X-API-Key` header)
- `docker-compose.gpu.yml` override file para GPU opcional
- CONTRIBUTING.md para contribuciones públicas

### Changed
- `docker-compose.yml` default a CPU (sin `runtime: nvidia`)
- CI actualizado: `actions/upload-artifact` v3→v4, service containers para Redis/RabbitMQ

### Fixed
- CI roto por `actions/upload-artifact@v3` deprecated
- Bare `except:` en tests e2e y docling-server
- URL incorrecta en README (`anomalyco/textflow` → `sudebaker/textFlow`)

## [0.1.0] - 2026-07-20

### Added
- Event-driven microservices architecture: Go orchestrator + Python workers
- Workers: extraction (Docling), embeddings (bge-m3), entities (GLiNER), metadata, inference (vLLM), completion, audio (Whisper), image (multimodal LLM)
- RabbitMQ broker con thread-safe channel pool y publisher confirms
- Redis client con TTL propagation y reconnection
- REST API con upload, polling, SSE streaming, batch processing
- SSRF protection, rate limiting, circuit breaker middleware
- Prometheus metrics: jobs_total, job_duration_seconds, queue_depth, http_requests
- Health checks para Redis y RabbitMQ
- Air-gapped deployment: 100% offline tras descarga inicial de modelos
- Docker Compose con healthchecks para todos los servicios
- BaseWorker/BaseAsyncWorker/BasePubSubWorker unificación para Python workers
- Adaptive Flow Control: admission control, RabbitMQ queue limits, AIMD semaphore
- Inference cache (Redis) para evitar llamadas redundantes al LLM
- Race condition fixes en inference assembly y pubsub reliability

### Security
- Non-root Dockerfiles para todos los servicios
- Credenciales RabbitMQ con placeholders `CHANGE_ME_*` + validación
- CORS configurable via `CORS_ORIGINS_LIST`
- `deploy/docker/.env` des-trackeado de git

## Guía de versiones

- **MAJOR:** cambios incompatibles en la API
- **MINOR:** nueva funcionalidad backward-compatible
- **PATCH:** bug fixes backward-compatible
