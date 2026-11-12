# textFlow — Security

## Superficie (readiness §24 / §29)

- HTTP `POST /v1/documents` (file / `document_url` download), webhook `WEBHOOK_URL`.
- RabbitMQ (`amqp://`), Redis (`redis://`), Docling HTTP (`DOCLING_URL`), LLM HTTP (`INFERENCE_BASE_URL`, `OCR_BASE_URL`/`IMAGE_BASE_URL`, `MULTIMODAL_LLM_URLS`), embeddings HTTP (`EMBEDDINGS_BASE_URL`), Whisper HTTP.
- Volúmenes: `models-data` (modelos, read-only), `artifacts-data`, `results-data`, `uploads-data`.

## SSRF y descarga

- `DocumentURL` validado: rechaza `127.0.0.1`, loopback, private, metadata (`169.254.169.254`, `metadata.google.internal`), y control `RABBITMQ_URL` interno (`internal: true` en redes Docker — `image-analyzer` requiere `docker_default` para `host.docker.internal`).
- Descargas atómicas (`FSStore` via `tmp` + `chmod 0644` + `rename`), timeouts `30s`/`LLM_TIMEOUT`.

## Webhooks e ingesta

- `handlers/batch.go:validateWebhookURL` rechaza loopback/private/metadata, solo `http|https`.
- Límite `MAX_SPREADSHEET_ROWS=2000`, `MAX_SPREADSHEET_SIZE_MB=5`, `MAX_AUDIO_SIZE_MB=500`.
- Features: solo se aceptan `inferences` e `inference_embeddings` (whitelist); no hay límite de número ni de longitud.

## Redis / RabbitMQ

- Redis runs with `maxmemory 1GB` and `noeviction`, is not published to the
  host, and should not be exposed. Set an ACL/`requirepass` if the network is
  shared.
- RabbitMQ is not published to the host. Management UI access, if enabled, must
  stay inside the VPN/bastion. Change `RABBITMQ_USER`/`RABBITMQ_PASS` (never
  `guest:guest`).

## Network isolation

- `backend` and `datastore` are `internal: true`: workers cannot reach the
  internet.
- `frontend` exposes the orchestrator; `docker_default` is the only network with
  host egress and is reserved for services that must reach operator backends
  (`image-analyzer`, `whisper`, and the LLM/embeddings clients).
- Only the orchestrator (8080), resource-manager (9090), Docling (8000),
  Prometheus (9091) and Grafana (3000) publish host ports. Redis and RabbitMQ
  do not.

## Secrets y env

- `RABBITMQ_PASS` via `.env` (nunca commitear). El secreto del webhook **no** es variable global: viaja por petición (`webhook_secret`) y firma cada callback con HMAC-SHA256. `deploy/package/package.sh` copia `.env.example` como plantilla, no `.env`.
- `ARTIFACT_PATH` (`/app/data/artifacts`) con `0755`/`0644` en `FSStore` (downstream lee con UID distinto).

## Actualizaciones

- Pinning `image:tag@sha256:digest` (o digest en `dist/MANIFEST.txt`) — `package.sh` lo registra con `docker inspect RepoDigests` o ID corto fallback.
