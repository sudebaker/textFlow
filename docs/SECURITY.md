# textFlow — Security

## Superficie (readiness §24 / §29)

- HTTP `POST /v1/documents` (file / `document_url` download), webhook `WEBHOOK_URL`.
- RabbitMQ (`amqp://`), Redis (`redis://`), Docling HTTP (`DOCLING_URL`), LLM HTTP (`LLM_URL`, `MULTIMODAL_LLM_URLS`), Whisper HTTP.
- Volumenes: `MODELS_PATH`, `artifacts-data`.

## SSRF y descarga

- `DocumentURL` validado: rechaza `127.0.0.1`, loopback, private, metadata (`169.254.169.254`, `metadata.google.internal`), y control `RABBITMQ_URL` interno (`internal: true` en redes Docker — `image-analyzer` requiere `docker_default` para `host.docker.internal`).
- Descargas atómicas (`FSStore` via `tmp` + `chmod 0644` + `rename`), timeouts `30s`/`LLM_TIMEOUT`.

## Webhooks e ingesta

- `handlers/batch.go:validateWebhookURL` rechaza loopback/private/metadata, solo `http|https`.
- Límite `MAX_SPREADSHEET_ROWS=2000`, `MAX_SPREADSHEET_SIZE_MB=5`, `MAX_AUDIO_SIZE_MB=500`.
- `MAX_FEATURES_PER_JOB=2`, `MAX_FEATURE_NAME_LENGTH=50`.

## Redis / RabbitMQ

- Redis: `maxmemory 1GB + noeviction` (evita evicción de refs), sin Internet, no exponer `6379` al host. Considerar ACL `requirepass`.
- RabbitMQ: imagen `rabbitmq:3.13-management` (pinned), `x-delayed-message` plugin para retry. Cambiar `RABBITMQ_USER/PASS` (nunca `guest:guest`) y no exponer management fuera de la VPN/bastión.

## Red interna

- `internal: true` en `docker-compose.yml` para `extract_text, embeddings, entities, metadata, inferences` networks — aísla workers de egress. `image-analyzer` y `whisper` explícitamente en `docker_default` para alcanzar el backend LLM/host.

## Secrets y env

- `RABBITMQ_PASS`, `WEBHOOK_SECRET` via `.env` (nunca commitear). `deploy/package/package.sh` copia `.env.example` como plantilla, no `.env`.
- `ARTIFACT_PATH` (`/app/data/artifacts`) con `0755`/`0644` en `FSStore` (downstream lee con UID distinto).

## Actualizaciones

- Pinning `image:tag@sha256:digest` (o digest en `dist/MANIFEST.txt`) — `package.sh` lo registra con `docker inspect RepoDigests` o ID corto fallback.
