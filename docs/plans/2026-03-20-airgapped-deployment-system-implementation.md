# Air-Gapped Deployment System Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a three-script deployment system (`package.sh`, `deploy.sh`, `install.sh`) that packages Docker images + models into a transferable bundle and automates setup on air-gapped target machines.

**Architecture:** Three independent scripts with clear responsibilities:
- `package.sh` (build machine): builds images, tars them individually, bundles models, generates manifest
- `deploy.sh` (build machine): rsync bundle to target via internal network
- `install.sh` (target machine): loads images, extracts models, configures .env, starts docker compose

**Tech Stack:** Bash 4.0+, Docker, rsync, jq (for digest parsing), sha256sum

---

## Task 1: Create `deploy/package/package.sh`

**Files:**
- Create: `deploy/package/package.sh`

**Step 1: Write the script stub with option parsing**

Create the file with basic structure for `--skip-build` flag:

```bash
#!/bin/bash
set -euo pipefail

SKIP_BUILD=false

# Parse arguments
while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-build)
      SKIP_BUILD=true
      shift
      ;;
    *)
      echo "Usage: $0 [--skip-build]"
      exit 1
      ;;
  esac
done

echo "Build images: $SKIP_BUILD"
```

Make it executable: `chmod +x deploy/package/package.sh`

**Step 2: Add the build images function**

```bash
build_images() {
  echo "======================================"
  echo "Building Docker images (GPU enabled)..."
  echo "======================================"
  
  docker compose \
    -f deploy/docker/docker-compose.yml \
    -f deploy/docker/docker-compose.gpu.yml \
    build --progress=plain
  
  echo "✓ All images built successfully"
}
```

Insert this before the "Parse arguments" section.

**Step 3: Add the docker save function**

```bash
save_images() {
  echo "======================================"
  echo "Saving Docker images to tarballs..."
  echo "======================================"
  
  mkdir -p dist/images
  
  # List of services to export (from docker-compose.yml)
  local services=(
    "orchestrator"
    "embeddings-worker"
    "entities-worker"
    "extraction-worker"
    "metadata-worker"
    "completion-worker"
    "resource-manager"
    "regex-entity-extractor"
  )
  
  # External images
  local external_images=(
    "rabbitmq:3.12-management"
    "redis:7-alpine"
    "quay.io/docling-project/docling-serve:latest"
  )
  
  # Save project services
  for service in "${services[@]}"; do
    local image="ia-text-${service}:latest"
    echo "  Exporting $image..."
    docker save "$image" | gzip > "dist/images/${service}.tar.gz"
  done
  
  # Save external images
  for image in "${external_images[@]}"; do
    local name=$(echo "$image" | sed 's|.*/||;s|:|-|')
    echo "  Exporting $image..."
    docker save "$image" | gzip > "dist/images/${name}.tar.gz"
  done
  
  echo "✓ All images saved to dist/images/"
}
```

**Step 4: Add the models bundling function**

```bash
bundle_models() {
  echo "======================================"
  echo "Bundling models (~11 GB)..."
  echo "======================================"
  
  if [[ ! -d "models" ]]; then
    echo "ERROR: models/ directory not found"
    echo "Run 'make setup-models' first"
    exit 1
  fi
  
  tar czf dist/models.tar.gz models/
  
  echo "✓ Models bundled to dist/models.tar.gz"
  ls -lh dist/models.tar.gz
}
```

**Step 5: Add config copying and manifest generation functions**

```bash
copy_config() {
  echo "======================================"
  echo "Copying config files..."
  echo "======================================"
  
  mkdir -p dist/config
  
  cp deploy/docker/docker-compose.yml dist/config/
  cp deploy/docker/docker-compose.gpu.yml dist/config/
  cp .env.example dist/config/
  
  cp deploy/package/install.sh dist/
  
  echo "✓ Config files copied"
}

generate_manifest() {
  echo "======================================"
  echo "Generating MANIFEST.txt..."
  echo "======================================"
  
  local manifest="dist/MANIFEST.txt"
  
  cat > "$manifest" << 'EOF'
IA Text Orchestrator - Air-Gapped Deployment Bundle
Generated: $(date -u '+%Y-%m-%d %H:%M:%S UTC')
Build host: $(hostname) ($(uname -s) $(uname -r), Docker $(docker --version | cut -d' ' -f3))

DOCKER IMAGES
─────────────
EOF
  
  # Get image digests
  for image in $(docker images --format "{{.Repository}}:{{.Tag}}" | grep "ia-text-\|rabbitmq\|redis\|docling"); do
    if [[ "$image" != "<none>:<none>" ]]; then
      local digest=$(docker inspect "$image" --format='{{.Id}}' | cut -d':' -f2 | cut -c1-12)
      echo "$image@$digest" >> "$manifest"
    fi
  done
  
  echo "" >> "$manifest"
  echo "MODELS (~11 GB)" >> "$manifest"
  echo "───────────────" >> "$manifest"
  local models_sha=$(sha256sum dist/models.tar.gz | cut -d' ' -f1)
  local models_size=$(du -h dist/models.tar.gz | cut -f1)
  echo "models.tar.gz: $models_sha ($models_size)" >> "$manifest"
  
  echo "" >> "$manifest"
  echo "INSTALLATION" >> "$manifest"
  echo "─────────────" >> "$manifest"
  cat >> "$manifest" << 'EOF'
On target, after transfer:
  bash ~/ia-text-deployment/install.sh
EOF
  
  cat "$manifest"
}
```

**Step 6: Wire up the main function**

Add this at the end of the script:

```bash
main() {
  # Clean old dist
  rm -rf dist
  mkdir -p dist
  
  if [[ "$SKIP_BUILD" != "true" ]]; then
    build_images
  fi
  
  save_images
  bundle_models
  copy_config
  generate_manifest
  
  echo ""
  echo "======================================"
  echo "✓ Deployment bundle ready at:"
  echo "  dist/"
  echo "======================================"
  echo ""
  echo "Next step:"
  echo "  make deploy HOST=<target-ip>"
}

main
```

**Step 7: Test locally**

Run: `bash deploy/package/package.sh --skip-build`
Expected: Should create `dist/` directory with error about models.tar.gz (because we haven't built images yet — that's OK)

Run: `make setup-models` (if models don't exist)
Run: `bash deploy/package/package.sh` (full build)
Expected: Takes 10–15 minutes, produces ~32 GB dist/ directory

**Step 8: Commit**

```bash
git add deploy/package/package.sh
git commit -m "feat: Add package.sh script for bundling images and models"
```

---

## Task 2: Create `deploy/package/deploy.sh`

**Files:**
- Create: `deploy/package/deploy.sh`

**Step 1: Write the script with HOST validation**

```bash
#!/bin/bash
set -euo pipefail

# Check HOST argument
if [[ -z "${1:-}" ]]; then
  echo "Usage: $0 <host>"
  echo "Example: $0 10.0.0.5"
  echo "         $0 user@10.0.0.5"
  exit 1
fi

HOST="$1"

# Validate dist/ exists
if [[ ! -d "dist/ia-text-deployment" ]]; then
  echo "ERROR: dist/ia-text-deployment/ not found"
  echo "Run 'make package' first"
  exit 1
fi
```

**Step 2: Add rsync transfer function**

```bash
transfer_bundle() {
  echo "======================================"
  echo "Transferring to $HOST..."
  echo "======================================"
  echo ""
  
  # Determine target user
  local target_path="~/ia-text-deployment"
  if [[ "$HOST" =~ @ ]]; then
    target_path="${HOST#*@}:~/ia-text-deployment"
  else
    target_path="$HOST:~/ia-text-deployment"
  fi
  
  # rsync with progress and resume capability
  rsync -avz \
    --progress \
    --delete-after \
    "dist/ia-text-deployment/" \
    "${target_path}/"
  
  echo ""
  echo "✓ Transfer complete"
}

install_on_target() {
  echo ""
  echo "======================================"
  echo "Next steps on target:"
  echo "======================================"
  echo ""
  echo "  ssh $HOST"
  echo "  bash ~/ia-text-deployment/install.sh"
  echo ""
}

main() {
  transfer_bundle
  install_on_target
}

main
```

Make executable: `chmod +x deploy/package/deploy.sh`

**Step 3: Test with a dry-run**

Run: `rsync -avz --dry-run dist/ia-text-deployment/ user@localhost:/tmp/test-deploy/`
Expected: Lists files without actual transfer

Run: `bash deploy/package/deploy.sh localhost` (if you have SSH to yourself configured)
Or: Skip this test, will verify in Task 5 (E2E test)

**Step 4: Commit**

```bash
git add deploy/package/deploy.sh
git commit -m "feat: Add deploy.sh script for rsync transfer to target"
```

---

## Task 3: Create `deploy/package/install.sh`

**Files:**
- Create: `deploy/package/install.sh`

**Step 1: Write the validation section**

```bash
#!/bin/bash
set -euo pipefail

echo "======================================"
echo "IA Text Orchestrator - Installation"
echo "======================================"
echo ""

# Check prerequisites
check_prerequisites() {
  echo "Checking prerequisites..."
  
  local missing=0
  
  if ! command -v docker &> /dev/null; then
    echo "✗ Docker not found"
    missing=1
  else
    echo "✓ Docker: $(docker --version)"
  fi
  
  if ! command -v nvidia-smi &> /dev/null; then
    echo "⚠ nvidia-smi not found (GPU required for embeddings/entities)"
  else
    echo "✓ GPU: $(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)"
  fi
  
  # Check disk space
  local available_gb=$(df . | awk 'NR==2 {printf "%.0f", $4 / 1024 / 1024}')
  echo "✓ Disk space available: ${available_gb} GB"
  if [[ $available_gb -lt 40 ]]; then
    echo "✗ Warning: Less than 40 GB free"
    missing=1
  fi
  
  if [[ $missing -eq 1 ]]; then
    echo ""
    echo "Missing critical prerequisites"
    exit 1
  fi
  
  echo "✓ All prerequisites met"
  echo ""
}

check_prerequisites
```

**Step 2: Add docker load function**

```bash
load_images() {
  echo "======================================"
  echo "Loading Docker images..."
  echo "======================================"
  
  if [[ ! -d "images" ]]; then
    echo "ERROR: images/ directory not found"
    exit 1
  fi
  
  local count=0
  for tar in images/*.tar.gz; do
    if [[ -f "$tar" ]]; then
      local image_name=$(basename "$tar" .tar.gz)
      echo "  Loading $image_name..."
      docker load -i "$tar" 2>&1 | grep -E "Loaded image|already exists" || true
      ((count++)) || true
    fi
  done
  
  echo "✓ Loaded $count images"
  echo ""
}
```

**Step 3: Add models extraction function**

```bash
extract_models() {
  echo "======================================"
  echo "Extracting models..."
  echo "======================================"
  
  if [[ ! -f "models.tar.gz" ]]; then
    echo "ERROR: models.tar.gz not found"
    exit 1
  fi
  
  # Extract to same directory (creates models/ subdirectory)
  tar xzf models.tar.gz
  
  echo "✓ Models extracted"
  du -sh models/ 2>/dev/null || echo "  (~11 GB)"
  echo ""
}
```

**Step 4: Add .env configuration function**

```bash
configure_env() {
  echo "======================================"
  echo "Configuring .env..."
  echo "======================================"
  
  if [[ -f ".env" ]]; then
    echo "  .env already exists, skipping"
  else
    if [[ ! -f "config/.env.example" ]]; then
      echo "ERROR: config/.env.example not found"
      exit 1
    fi
    
    cp config/.env.example .env
    echo "  Created .env from .env.example"
  fi
  
  # For air-gapped deployment, set offline mode
  sed -i 's/^HF_HUB_OFFLINE=.*/HF_HUB_OFFLINE=1/' .env
  sed -i 's/^TRANSFORMERS_OFFLINE=.*/TRANSFORMERS_OFFLINE=1/' .env
  sed -i 's/^ALLOW_REMOTE_DOWNLOAD=.*/ALLOW_REMOTE_DOWNLOAD=false/' .env
  
  echo "✓ Air-gapped mode enabled in .env"
  echo ""
}
```

**Step 5: Add volumes and permissions setup**

```bash
setup_volumes() {
  echo "======================================"
  echo "Creating Docker volumes..."
  echo "======================================"
  
  docker volume create redis-data 2>/dev/null || echo "  redis-data already exists"
  docker volume create rabbitmq-data 2>/dev/null || echo "  rabbitmq-data already exists"
  
  mkdir -p uploads-data results-data data entities-cache
  chmod 777 uploads-data results-data data entities-cache
  
  echo "✓ Volumes ready"
  echo ""
}
```

**Step 6: Add docker compose up**

```bash
start_services() {
  echo "======================================"
  echo "Starting services..."
  echo "======================================"
  
  docker compose \
    -f config/docker-compose.yml \
    -f config/docker-compose.gpu.yml \
    up -d
  
  echo "✓ Docker Compose started"
  echo ""
}
```

**Step 7: Add health check**

```bash
wait_for_health() {
  echo "======================================"
  echo "Waiting for services to be ready..."
  echo "======================================"
  
  local max_attempts=12
  local attempt=0
  
  while [[ $attempt -lt $max_attempts ]]; do
    if curl -s http://localhost:8080/health 2>/dev/null | grep -q '"status":"ok"'; then
      echo "✓ Orchestrator is healthy"
      echo ""
      return 0
    fi
    
    echo "  Waiting... ($((attempt+1))/$max_attempts)"
    sleep 5
    ((attempt++)) || true
  done
  
  echo "⚠ Orchestrator did not become healthy within $(($max_attempts * 5))s"
  echo "  Check logs: docker compose logs orchestrator"
}
```

**Step 8: Add final status output**

```bash
print_status() {
  echo "======================================"
  echo "✓ Deployment complete!"
  echo "======================================"
  echo ""
  echo "Services available:"
  echo "  - Orchestrator:    http://localhost:8080"
  echo "  - RabbitMQ Admin:  http://localhost:15672 (guest/guest)"
  echo "  - Redis:           localhost:6379"
  echo "  - Docling:         http://localhost:8000"
  echo ""
  echo "Verify pipeline:"
  echo "  tools/client/client -i <document> -o result.json"
  echo ""
}

main() {
  check_prerequisites
  load_images
  extract_models
  configure_env
  setup_volumes
  start_services
  wait_for_health
  print_status
}

main
```

Make executable: `chmod +x deploy/package/install.sh`

**Step 9: Test locally**

Run on the same machine (simulates target):
```bash
cd /tmp/test-install
mkdir -p images config
# Copy dummy tar files or skip
bash /path/to/deploy/package/install.sh
```

Expected: Will fail on docker load (no images), but validates structure

**Step 10: Commit**

```bash
git add deploy/package/install.sh
git commit -m "feat: Add install.sh for target machine setup"
```

---

## Task 4: Update Makefile

**Files:**
- Modify: `Makefile`

**Step 1: Add the new targets**

Find the "Docker / Deploy" section in the Makefile and add these targets:

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

Insert after the existing `docker-logs` target.

**Step 2: Update .gitignore**

Add to `.gitignore`:
```
dist/
```

**Step 3: Test the new targets**

Run: `make package-skip-build`
Expected: Creates dist/ without building images

Run: `make` (or `make help`)
Expected: Shows package, package-skip-build, deploy, install-remote targets

**Step 4: Commit**

```bash
git add Makefile .gitignore
git commit -m "feat: Add Makefile targets for deployment packaging"
```

---

## Task 5: Integration Test — Local Package & Extract

**Files:**
- Create: `tests/deployment/test_package.sh`

**Step 1: Create the test script**

```bash
#!/bin/bash
set -euo pipefail

# Test that package.sh produces valid output

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

echo "Testing package.sh..."

# Skip image build for speed (assume already built)
bash deploy/package/package.sh --skip-build

# Validate dist structure
test -d dist/images || exit 1
test -d dist/config || exit 1
test -f dist/models.tar.gz || exit 1
test -f dist/MANIFEST.txt || exit 1
test -f dist/install.sh || exit 1

echo "✓ dist structure valid"

# Validate tar files can be listed
tar tzf dist/models.tar.gz | head -5 > /dev/null || exit 1
echo "✓ models.tar.gz is readable"

# Validate config files present
test -f dist/config/docker-compose.yml || exit 1
test -f dist/config/docker-compose.gpu.yml || exit 1
test -f dist/config/.env.example || exit 1

echo "✓ All config files present"
echo "✓ Package test passed"
```

Make executable: `chmod +x tests/deployment/test_package.sh`

**Step 2: Create directory if needed**

```bash
mkdir -p tests/deployment
```

**Step 3: Run the test**

Run: `bash tests/deployment/test_package.sh`
Expected: Validates dist/ structure and tars are readable

**Step 4: Add to CI (optional for now)**

Can be integrated into GitHub Actions later.

**Step 5: Commit**

```bash
git add tests/deployment/test_package.sh
git commit -m "test: Add integration test for package.sh"
```

---

## Task 6: Documentation

**Files:**
- Create: `docs/AIRGAPPED_DEPLOYMENT.md`

**Step 1: Write the deployment guide**

```markdown
# Air-Gapped Deployment Guide

## Overview

This guide walks through packaging the IA Text Orchestrator for deployment to an air-gapped (internet-isolated) machine.

## Prerequisites

### Build Machine
- Docker 20.10+ with GPU support (`docker run --gpus all` works)
- NVIDIA Container Toolkit (for GPU image builds)
- Bash 4.0+, rsync, curl

### Target Machine
- Docker 20.10+ installed
- NVIDIA Container Toolkit installed
- 40+ GB free disk space
- NVIDIA GPU (RTX 4080 or similar for embeddings/entities)
- Internal network access to build machine (or USB/external drive)

## Quick Start

### 1. Build and Package (on build machine)

```bash
# Build all Docker images and bundle models (~30–40 GB)
make package

# Validate the bundle
ls -lh dist/ia-text-deployment/
cat dist/MANIFEST.txt
```

This produces:
- `dist/images/*.tar.gz` — 11 Docker images
- `dist/models.tar.gz` — all models, embeddings, NER weights (~11 GB)
- `dist/config/` — docker-compose files and .env template
- `dist/install.sh` — target machine setup script

### 2. Transfer to Target (over internal network)

```bash
# Transfer via rsync
make deploy HOST=10.0.0.5

# Or manually:
rsync -avz --progress dist/ia-text-deployment/ user@10.0.0.5:~/ia-text-deployment/
```

Typical transfer time: 20–45 minutes (depends on network speed and disk I/O).

### 3. Install on Target (run on target machine)

```bash
ssh user@10.0.0.5
cd ~/ia-text-deployment
bash install.sh
```

The script will:
- Load all Docker images
- Extract models (~11 GB)
- Configure .env for offline mode
- Create Docker volumes
- Start all services with docker-compose
- Wait for orchestrator to be healthy (60s timeout)

On success:
```
✓ Deployment complete!

Services available:
  - Orchestrator:    http://localhost:8080
  - RabbitMQ Admin:  http://localhost:15672
  - Docling:         http://localhost:8000
```

### 4. Verify the Pipeline

Use the CLI client (on target or networked to target):

```bash
cd /path/to/ia-text-orchestrator
tools/client/client -i test.pdf -o result.json -u http://10.0.0.5:8080
```

Expected output: JSON file with extracted text, embeddings, entities.

## Troubleshooting

### Docker images won't load: "permission denied"

**Solution:** Ensure docker daemon is running and your user is in the docker group.

```bash
docker ps  # Should work without sudo
```

### Models extract but services won't start: "out of disk space"

**Solution:** Check available space.

```bash
df -h /
# Need at least 40 GB free; extract happens in-place
```

### Orchestrator not healthy after 60s

**Solution:** Check logs.

```bash
docker compose logs orchestrator
docker compose logs embeddings-worker
```

Common issues:
- GPU driver not installed: `nvidia-smi` should work
- Out of VRAM: RTX 4080 has 12 GB; reduce batch size in .env if needed
- Models not found: Verify `models/` extracted fully

### install.sh fails mid-way

**Solution:** Re-run it. It's idempotent.

```bash
bash install.sh  # Safe to re-run
```

## Advanced: Incremental Updates

If you need to push just new code (not all models again):

```bash
# On build machine, rebuild just one service
docker compose -f deploy/docker/docker-compose.yml -f deploy/docker/docker-compose.gpu.yml build orchestrator

# Save just that image
docker save ia-text-orchestrator:latest | gzip > dist/images/orchestrator.tar.gz

# Transfer just the image
rsync -avz dist/images/orchestrator.tar.gz user@10.0.0.5:~/ia-text-deployment/images/

# On target
docker load -i ~/ia-text-deployment/images/orchestrator.tar.gz
docker compose -f ~/ia-text-deployment/config/docker-compose.yml -f ~/ia-text-deployment/config/docker-compose.gpu.yml up -d orchestrator
```

## What Gets Packaged

| Component | Size | Notes |
|-----------|------|-------|
| Docker Images (11 total) | ~32 GB | Includes all Go and Python services, infrastructure (RabbitMQ, Redis, Docling) |
| Models | ~11 GB | BAAI/bge-m3, GLiNER, DeBERTa, Docling artifacts, HuggingFace cache |
| Config files | < 1 MB | docker-compose.yml, .env.example |
| **Total** | **~43 GB** | Compressed somewhat during transfer with rsync |

## Offline Mode

The deployment automatically enables offline mode in .env:

```env
HF_HUB_OFFLINE=1
TRANSFORMERS_OFFLINE=1
ALLOW_REMOTE_DOWNLOAD=false
```

This prevents the system from attempting to download anything from HuggingFace Hub at runtime. All models are bundled in `models.tar.gz`.

## Rollback / Cleanup

To remove the deployment:

```bash
cd ~/ia-text-deployment
docker compose -f config/docker-compose.yml -f config/docker-compose.gpu.yml down -v

# Remove volumes
docker volume rm redis-data rabbitmq-data

# Remove extracted models (reclaims ~11 GB)
rm -rf models/
```

## See Also

- docs/API.md — REST API endpoints
- docs/GLINER_OFFLINE_MODE.md — GLiNER offline specifics
- AGENTS.md — Build commands, code style
```

Save to: `docs/AIRGAPPED_DEPLOYMENT.md`

**Step 2: Commit**

```bash
git add docs/AIRGAPPED_DEPLOYMENT.md
git commit -m "docs: Add comprehensive air-gapped deployment guide"
```

---

## Task 7: Cleanup and Final Verification

**Step 1: Remove stray orchestrator binary**

The file `deploy/docker/orchestrator` is a leftover compiled binary. Remove it if it exists:

```bash
ls -la deploy/docker/orchestrator
# If it's a binary file (not a directory), remove it
file deploy/docker/orchestrator
if file deploy/docker/orchestrator | grep -q "ELF"; then
  rm deploy/docker/orchestrator
  git add deploy/docker/orchestrator
  git commit -m "chore: Remove stray orchestrator ELF binary from deploy/docker/"
fi
```

**Step 2: Verify all scripts are executable**

```bash
chmod +x deploy/package/*.sh
git diff HEAD -- deploy/package/*.sh | grep "^diff" || echo "✓ Scripts already executable"
```

**Step 3: Verify git status is clean**

```bash
git status
# Should show nothing or only untracked files
```

Expected: All tasks committed.

**Step 4: Create a summary**

```bash
git log --oneline -10
```

Expected: Shows the commits from this implementation:
- Add air-gapped deployment system design spec
- Add package.sh script for bundling images and models
- Add deploy.sh script for rsync transfer to target
- Add install.sh for target machine setup
- Add Makefile targets for deployment packaging
- Add integration test for package.sh
- Add comprehensive air-gapped deployment guide
- [Optional] Remove stray orchestrator ELF binary

---

## Success Criteria

✓ All three scripts exist, are executable, and have clear comments  
✓ `make package` produces valid dist/ directory (~32–35 GB)  
✓ `make deploy HOST=...` transfers via rsync  
✓ `install.sh` is idempotent and validates prerequisites  
✓ Full end-to-end: package → transfer → install → verify pipeline works  
✓ MANIFEST.txt has checksums and image digests  
✓ Deployment guide covers quick start, troubleshooting, rollback  
✓ All changes committed to git  

---

## Timeline

| Task | Est. Time |
|------|-----------|
| 1. package.sh | 30 min |
| 2. deploy.sh | 15 min |
| 3. install.sh | 45 min |
| 4. Makefile | 10 min |
| 5. Integration test | 15 min |
| 6. Documentation | 30 min |
| 7. Cleanup & verify | 15 min |
| **Total** | **2.5–3 hours** |

---

## Notes for Implementation

- **TDD where practical:** install.sh has many conditional steps; test with `set -x` for debugging
- **Shell safety:** All scripts use `set -euo pipefail` to fail fast on errors
- **Idempotency:** `docker load`, `docker volume create`, `docker compose up -d` are all idempotent
- **No secrets in git:** .env.example is tracked, but generated .env should be .gitignore'd
- **Test incremental:** After each task, run the script manually to validate before committing

---

## References

- docs/plans/2026-03-20-airgapped-deployment-system-design.md — design spec
- AGENTS.md — project structure, build commands
- Makefile — existing targets to extend
