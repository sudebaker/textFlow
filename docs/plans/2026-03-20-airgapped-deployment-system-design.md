# Design: Air-Gapped Deployment System for IA Text Orchestrator

**Date:** 2026-03-20  
**Status:** Design approved, ready for implementation  
**Scope:** One-time deployment to air-gapped NVIDIA GPU machine via internal network

---

## Goals

Build a robust deployment system that:
- Packages all Docker images, models (~11 GB), and config into a portable bundle
- Transfers the bundle to an air-gapped target via isolated internal network (rsync)
- Automates setup on the target with a single `install.sh` script (idempotent)
- Provides visibility into what's being deployed (MANIFEST.txt with digests, checksums)
- Allows re-runs without data loss or side effects

---

## Context

### Current State
- **Images:** 11 services, built from Dockerfiles in `cmd/*/`, GPU support via compose override
- **Models:** ~11 GB total (bge-m3 flat dir + HF cache, deberta, gliner, docling)
- **Deployment:** Docker Compose (docker-compose.yml + docker-compose.gpu.yml)
- **Gaps:** No `make package`, no `docker save`, no transfer/install scripts

### Target Environment
- Air-gapped machine on isolated internal network (no internet access)
- Docker + NVIDIA Container Toolkit already installed
- Single machine, one-time deployment
- GPU: NVIDIA (embeddings, entities, docling require GPU)

---

## Solution: Option B — Structured `deploy/package/` System

### Architecture

```
Build Machine                    Isolated Network               Target Machine
─────────────                    ────────────────               ──────────────
1. make package          ──rsync──>     dist/ia-text-    ─────> 2. install.sh
   (builds images)                     deployment/
   (tars models)
   (creates MANIFEST)
                         
2. make deploy           ──rsync──>     (transfer only)  ─────> 3. docker compose up -d
   HOST=10.0.0.5                      
                         
3. make install-remote   ───SSH───>     (executes)      ─────> 4. Health check
   HOST=10.0.0.5
```

### New Directory: `deploy/package/`

Three scripts, one purpose each:

#### `deploy/package/package.sh`
Runs on **build machine**. Produces `dist/ia-text-deployment/` bundle.

**Steps:**
1. Build all Docker images using GPU compose override (or skip with `--skip-build`)
2. `docker save` each image to `dist/images/<service>.tar`
3. `tar czf dist/models.tar.gz models/` (all models, ~11 GB)
4. Copy compose files and `.env.example` to `dist/config/`
5. Copy `install.sh` to `dist/` root
6. Generate `MANIFEST.txt`:
   - Image digests (via `docker inspect`)
   - Model checksums (sha256sum of models.tar.gz)
   - Build timestamp, host info

**Invocation:**
```bash
bash deploy/package/package.sh [--skip-build]
```

**Output:**
```
dist/ia-text-deployment/
├── images/
│   ├── orchestrator.tar
│   ├── embeddings-worker.tar
│   ├── entities-worker.tar
│   ├── extraction-worker.tar
│   ├── metadata-worker.tar
│   ├── completion-worker.tar
│   ├── resource-manager.tar
│   ├── regex-entity-extractor.tar
│   ├── rabbitmq.tar
│   ├── redis.tar
│   └── docling.tar
├── models.tar.gz           (~11 GB)
├── config/
│   ├── docker-compose.yml
│   ├── docker-compose.gpu.yml
│   └── .env.example
├── install.sh              (copy of deploy/package/install.sh)
├── MANIFEST.txt
└── README.txt              (usage instructions for target)
```

---

#### `deploy/package/deploy.sh`
Runs on **build machine**. Transfers bundle to target via rsync.

**Steps:**
1. Validate `HOST` is set and resolvable
2. `rsync -avz --progress --delete-after dist/ia-text-deployment/ user@HOST:~/ia-text-deployment/`
3. Print next step instructions: SSH command to run `install.sh`

**Invocation:**
```bash
bash deploy/package/deploy.sh 10.0.0.5
# or:
bash deploy/package/deploy.sh user@10.0.0.5
```

**Output:**
```
Transferring dist/ia-text-deployment/ → user@10.0.0.5:~/ia-text-deployment/ ...
[rsync progress with speed, ETA]
...

✓ Transfer complete (45 min, 32 GB).

Next step on target machine:
  ssh user@10.0.0.5
  bash ~/ia-text-deployment/install.sh
```

**Why rsync over scp:**
- Resume on interruption
- Live progress + speed/ETA
- Delta transfers (re-runs only transfer changes)
- `--delete-after` cleans old images if they were replaced

---

#### `deploy/package/install.sh`
Runs on **target machine**. Completes offline deployment.

**Steps:**
1. **Validation**
   - Check `docker` is available
   - Check `nvidia-container-toolkit` is installed
   - Verify sufficient disk space (at least 40 GB free)
   - Print current env summary

2. **Load Docker images**
   ```bash
   for tar in images/*.tar; do
     docker load -i "$tar"
   done
   ```
   (Idempotent: `docker load` skips if image digest already exists)

3. **Extract models**
   ```bash
   mkdir -p ~/ia-text-deployment/models
   tar xzf models.tar.gz -C ~/ia-text-deployment/
   ```

4. **Generate `.env`**
   - Copy `.env.example` → `.env`
   - Interactive prompt for non-default secrets (or accept defaults for air-gapped mode)
   - Example prompts:
     - `RABBITMQ_USER [default: guest]: `
     - `RABBITMQ_PASSWORD [default: guest]: `
     - `Models path [default: ./models]: `

5. **Setup volumes and permissions**
   ```bash
   docker volume create redis-data 2>/dev/null || true
   docker volume create rabbitmq-data 2>/dev/null || true
   mkdir -p uploads-data results-data data entities-cache
   ```

6. **Start the stack**
   ```bash
   docker compose \
     -f config/docker-compose.yml \
     -f config/docker-compose.gpu.yml \
     up -d
   ```

7. **Health check** (waits up to 60s)
   ```bash
   for i in {1..12}; do
     if curl -s http://localhost:8080/health | grep -q '"status":"ok"'; then
       echo "✓ Orchestrator healthy"
       break
     fi
     echo "Waiting for orchestrator... ($i/12)"
     sleep 5
   done
   ```

8. **Final status**
   ```
   ✓ Deployment complete!
   
   Services running:
     - Orchestrator:  http://localhost:8080
     - RabbitMQ:      http://localhost:15672
     - Redis:         localhost:6379
     - Docling:       http://localhost:8000
   
   To verify:
     make test-e2e
   ```

**Idempotency:**
- `docker load` is idempotent (no-op if digest matches)
- `docker volume create` ignores existing volumes
- `docker compose up -d` updates existing services or creates new ones (no state loss)
- Safe to re-run multiple times

**Error handling:**
- Exit with clear message if Docker/nvidia-smi not found
- Disk space check prevents mid-extract failures
- Log all docker load/compose output for debugging
- On any failure, print remediation steps

---

### Makefile Integration

Three new targets in the root Makefile:

```makefile
.PHONY: package deploy install-remote

package:
	@bash deploy/package/package.sh

package-skip-build:
	@bash deploy/package/package.sh --skip-build

deploy:
	@bash deploy/package/deploy.sh $(HOST)

install-remote:
	@bash deploy/package/install.sh
```

Usage:
```bash
# Build everything and create bundle
make package

# Transfer to target (requires HOST=...)
make deploy HOST=10.0.0.5

# On target: extract and start (standalone script, can also run manually via SSH)
ssh user@10.0.0.5 "bash ~/ia-text-deployment/install.sh"
```

---

### MANIFEST.txt Format

Example:
```
IA Text Orchestrator - Air-Gapped Deployment Bundle
Generated: 2026-03-20 14:32:15 UTC
Build host: ip-172-30-0-5 (Linux 5.15.0, Docker 24.0.7, NVIDIA Driver 545.23.06)

DOCKER IMAGES (11 total, ~32 GB)
─────────────────────────────────
orchestrator@sha256:a1b2c3d4...
embeddings-worker@sha256:e5f6g7h8...
entities-worker@sha256:i9j0k1l2...
extraction-worker@sha256:m3n4o5p6...
metadata-worker@sha256:q7r8s9t0...
completion-worker@sha256:u1v2w3x4...
resource-manager@sha256:y5z6a7b8...
regex-entity-extractor@sha256:c9d0e1f2...
rabbitmq:3.12-management@sha256:g3h4i5j6...
redis:7-alpine@sha256:k7l8m9n0...
docling:latest@sha256:o1p2q3r4...

MODELS (~/models/, ~11 GB)
──────────────────────────
models.tar.gz: sha256:1a2b3c4d5e6f7g8h9i0j (11.2 GB)
  └─ bge-m3/                   (4.3 GB)
  └─ deberta-v3-small/         (276 MB)
  └─ gliner-small-v2.1/        (583 MB)
  └─ docling/                  (705 MB)
  └─ huggingface_cache/        (5.2 GB)

CONFIGURATION
──────────────
docker-compose.yml (tracked)
docker-compose.gpu.yml (override, GPU support)
.env.example (configure on target)

REQUIREMENTS (target machine)
──────────────────────────────
✓ Docker 20.10+
✓ NVIDIA Container Toolkit
✓ 40 GB disk space
✓ NVIDIA GPU (embeddings, entities, docling)
✓ Internal network access (if transferring via rsync)

INSTALLATION
──────────────────────────────
On target, after transfer:
  bash ~/ia-text-deployment/install.sh
```

---

### Files to Create

1. `deploy/package/package.sh` — ~180 lines (build, docker save, tar, manifest)
2. `deploy/package/deploy.sh` — ~60 lines (rsync wrapper)
3. `deploy/package/install.sh` — ~250 lines (load, extract, config, health check)
4. Update root `Makefile` — add 4 targets

### Files to Modify

1. `.gitignore` — add `dist/` directory
2. `.env.example` — already exists, no changes needed

---

## Testing Strategy

1. **Unit tests (local)**
   - `package.sh` produces valid tars (verify with `tar tzf`)
   - MANIFEST checksums match (regenerate and compare)
   - `install.sh` can run twice without errors (idempotency)

2. **Integration test (single machine)**
   - Run `make package` on dev machine
   - Extract to a temp directory
   - Run `install.sh` on same machine, different docker socket/compose project name
   - Verify services start and health check passes

3. **End-to-end test (air-gapped simulation)**
   - Spin up a VM or air-gapped Linux machine
   - Transfer bundle via rsync
   - Run `install.sh`
   - Run CLI client (`tools/client/client`) to submit a test document
   - Verify full pipeline completes

---

## Success Criteria

- ✓ `make package` produces reproducible bundle (same digests on re-run)
- ✓ Bundle size documented and reasonable (~32–35 GB for images + models)
- ✓ `install.sh` is idempotent (can be re-run, no data loss)
- ✓ Health check confirms all services are ready before script exits
- ✓ E2E test passes: document in → results out
- ✓ MANIFEST.txt allows integrity verification on target
- ✓ Clear error messages if prerequisites missing (Docker, nvidia-smi, disk space)

---

## Timeline

| Phase | Tasks | Est. Duration |
|-------|-------|----------|
| Phase 1: Scripting | Write package.sh, deploy.sh, install.sh | 2–3 hours |
| Phase 2: Makefile | Add targets, update .gitignore, test locally | 30 min |
| Phase 3: Testing | Local unit + integration tests | 1–2 hours |
| Phase 4: Documentation | README, troubleshooting guide | 1 hour |
| **Total** | | **4–7 hours** |

---

## Rollback / Cleanup

If deployment fails mid-transfer:
- Re-run `make deploy HOST=...` (rsync will resume)
- On target, no cleanup needed (images are in Docker storage, not yet loaded)

If deployment fails mid-install:
- Re-run `bash ~/ia-text-deployment/install.sh` (idempotent)
- Or manually: `docker compose -f config/docker-compose.yml -f config/docker-compose.gpu.yml down` and re-run

If you need to revert:
- On target: `docker compose down -v` (removes all volumes)
- Or: manually remove volumes and docker images

---

## Future Enhancements

Not in scope for this design, but worth noting:

1. **Incremental updates** — detect which images/models changed, transfer only deltas
2. **Multi-machine deployment** — ansible playbook wrapper around the scripts
3. **Kubernetes** — Helm chart based on compose structure
4. **Rollback mechanism** — keep previous image versions, provide rollback script
5. **Automated testing** — CI/CD job that tests the entire package.sh → install.sh flow on a fresh VM each time

---

## References

- AGENTS.md: Build/test commands, code style, air-gapped requirements
- docs/API.md: Orchestrator health check endpoint
- docs/GLINER_OFFLINE_MODE.md: Offline model loading specifics
- Makefile: existing targets (setup-models, docker-build, infra-up, etc.)
