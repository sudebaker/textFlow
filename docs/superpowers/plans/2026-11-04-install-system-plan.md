# Plan: Sistema de instalación textflow (dev + air-gapped)

Spec fuente: conversación 2026-11-04 (plan aprobado por usuario "Adelante") + refinamiento del usuario:
**UN script que descarga modelos necesarios y el usuario ELIGE cuáles** (interactivo + flags).
Catálogo solo con modelos de fichero local; **los que se resuelven por endpoint (embeddings, LLM, vision) NO se descargan aquí** — el usuario los provee vía env vars.
Diseño aprobado: backends agnósticos, sin preset; .env único raíz; doctor + smoke-test; vendor de assets pequeños de modelos; alcance dev + bundle air-gapped.

## Principios
- Backends agnósticos: contrato en env vars validado fail-fast (doctor).
- Un solo .env: raíz canónica, compose vía --env-file.
- Idempotencia: re-runs baratos en todos los pasos.
- TDD en lógica testeable (python stdlib+pytest); validación real para partes docker/red.

## Modelo del catálogo (Task 2)
LOCAL (seleccionables): gliner (incluye deberta tokenizer + dirs planos via assets vendored + pesos HF), whisper (Systran/faster-whisper-large-v2 + seed models/whisper/), docling (artifacts via docker host).
ENDPOINT (no descargables, mostrados en --list como "(se resuelve por endpoint)"): embeddings, LLM de inferencia, vision/OCR.
CLI: `--list`, `--models gliner,whisper`, `--all`, interactivo por defecto (multi-select numerado).
Integridad: expectativas alineadas a upstream real (gliner: weights+gliner_config; deberta: config+spm+tokenizer_config; whisper: config+model.bin+tokenizer.json). Sin entradas duplicadas.

## Tasks

### Task 1 — Vendor assets de modelos pequeños
`deploy/models-assets/gliner-small-v2.1/` (config.json, tokenizer.json, tokenizer_config.json,
special_tokens_map.json, spm.model, added_tokens.json, gliner_config.json — NO pesos),
`deploy/models-assets/deberta-v3-small/special_tokens_map.json`, `SHA256SUMS`.
Fuente: staging actual `models/` (172-174 files ya verificados funcionando offline).
RED→GREEN: `deploy/docker/tests/test_model_assets.py` (pytest): existen todos, sha256 manifesto coincide, tamaño gliner dir < 15MB (sin pesos).
Commit: `chore(models): vendor small model assets (tokenizer/config) for offline staging`

### Task 2 — download_models.py (picker + catálogo containerizado)
Nuevo `deploy/docker/download_models.py` (reemplaza download_models_offline.py):
catálogo, parseo selección (RED→GREEN tests en `deploy/docker/tests/test_download_models.py`:
alias/unkown/coma/--all/--list; gliner ⇒ deberta como dependencia; exclusiones endpoint),
descarga snapshot_download idempotente, integridad real, MANIFEST.
Makefile `setup-models`: `docker run --rm -v $(PWD):/models python:3.10-slim` con deps pineadas;
`setup-models-list` passthrough. Borrar script viejo.
Commit: `feat(models): user-selectable staged downloader (containerized, endpoint-agnostic)`

### Task 3 — Ensamblado layout + docling
Assemblers en el mismo script: gliner flat dir (assets vendored + pesos del snapshot),
deberta flat dir (snapshot files + special_tokens_map vendored), whisper seed (hub dir + shard CAS).
RED→GREEN con sandbox tmp: idempotente, error claro si faltan pesos.
`deploy/scripts/extract_docling.sh` (host, docker create+cp). Makefile lo engancha si docling seleccionado.
Commit: `feat(models): assemble worker layouts from vendored assets + docling artifacts`

### Task 4 — .env único + ejemplo honesto
Makefile: todos los targets de compose con `--env-file .env` (raíz). Consolidar deploy/docker/.env → raíz y borrar copia.
`.env.example`: bloque REQUERIDO placeholders vacíos (EMBEDDINGS_*/INFERENCE_*), warning /v1,
EMBEDDINGS_DIMENSION documentado, bloque opcional multimodal. Commit fix redes de ayer.
Verificacación: `docker compose --env-file .env config` resuelve MODELS_PATH a repo/models.
Commit: `refactor(config): single canonical .env + honest backend contract example`

### Task 5 — make doctor
`deploy/scripts/doctor.py` (stdlib): checks docker/compose/.env/vars requeridas/URL sanity (sin /v1)/
staging (dirs planos, symlinks colgados)/volumen/backends alcanzables desde red contenedor (one-shot
docker run) con validez dims. Tabla PASS/FAIL + hints. Reemplaza verify-config.sh (borrar).
Tests: unit de check_* con fetcher inyectable + env fixture (RED→GREEN).
Commit: `feat(doctor): fail-fast preflight doctor replacing verify-config`

### Task 6 — make smoke-test
`deploy/scripts/smoke_test.py`: PDF mínimo, upload (client o /v1/documents/upload), poll,
assertions: completed, steps, dims==EMBEDDINGS_DIMENSION, entities/metadata. FAIL = diffs claros.
Tests: parsing/asserts con fixtures JSON (RED→GREEN); E2E en Task 10.
Commit: `feat(smoke): end-to-end smoke test with env-aware assertions`

### Task 7 — make bootstrap
Doctor(pre) → setup-models → models-sync → build → up → wait-healthy → doctor(post)/ → smoke-test.
SKIP_MODEL=1 etc. idempotente. bash -n verificado.
Commit: `feat(bootstrap): one-command bootstrap with idempotent skips`

### Task 8 — Docs
README Quick Start reescrita (prereqs, tamaños, trae-backends, comandos); borrar QUICKSTART.md y
deploy/docker/README.md obsoletos; docs/MODELS.md actualizado (layout vendored+CAS, exclusiones
endpoint); consolidar deployment-guide.md + AIRGAPPED_DEPLOYMENT.md (una canónica); AGENTS.md.
Commit: `docs(install): single-source install guide aligned with doctor/smoke`

### Task 9 — Bundle air-gapped
install.sh: escribir env canónico de bundle, llamar doctor/smoke-test (layout ../models),
verificar-bundle/verification alineados. package.sh sin cambio salvo integración doctor.
Commit: `feat(bundle): air-gapped installer aligned with new env + doctor/smoke`

### Task 10 — Validación real
Re-run idempotente full (setup-models --models gliner,whisper → docling → sync → doctor → smoke en stack vivo).
Test staging fresco via hardlink copy + MODELS_PATH tmp. bash -n todo suite python verde.
Commit(s) de arreglos que surjan: `fix(...)` correspondientes.

## Review Focus
- Picker catálogo: excluir endpoint models por diseño.
- Idempotencia y CAS symlink handling.
- No romper bundle air-gapped existente.
