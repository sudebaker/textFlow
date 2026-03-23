#!/usr/bin/env bash
# deploy/package/package.sh
# Produces a complete deployable bundle in dist/
# Usage: bash deploy/package/package.sh [--skip-build]
# Must be run from the repository root.

set -euo pipefail

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DIST_DIR="dist"
COMPOSE_BASE="deploy/docker/docker-compose.yml"
COMPOSE_GPU="deploy/docker/docker-compose.gpu.yml"
MODELS_DIR="models"
INSTALL_SRC="deploy/package/install.sh"

# Built images (project name = "docker" because compose files live in deploy/docker/)
BUILT_IMAGES=(
  "docker-orchestrator:latest"
  "docker-embeddings-worker:latest"
  "docker-entities-worker:latest"
  "docker-extraction-worker:latest"
  "docker-metadata-worker:latest"
  "docker-completion-worker:latest"
  "docker-resource-manager:latest"
  "docker-regex-entity-extractor:latest"
)

# External / pulled images
EXTERNAL_IMAGES=(
  "rabbitmq:3.12-management"
  "redis:7-alpine"
  "quay.io/docling-project/docling-serve:latest"
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log()  { echo "[package] $*"; }
warn() { echo "[package] WARNING: $*" >&2; }
die()  { echo "[package] ERROR: $*" >&2; exit 1; }

# Convert an image reference to a safe filename.
# Rules:
#   - strip registry host/org prefix (anything before last '/') using basename
#     on the tag-stripped portion, so registry:port/image:tag works correctly
#   - for built images (docker-* prefix): strip `:latest` from filename
#     e.g. docker-orchestrator:latest -> docker-orchestrator.tar.gz
#   - for external images: always include the tag
#     e.g. rabbitmq:3.12-management  -> rabbitmq-3.12-management.tar.gz
#     e.g. docling-serve:latest      -> docling-serve-latest.tar.gz
image_to_filename() {
  local img="$1"
  local no_tag base tag
  no_tag="${img%:*}"             # strip tag (last colon only)
  base=$(basename "$no_tag")     # strip registry/org prefix
  tag="${img##*:}"
  if [[ "$base" == docker-* && "$tag" == "latest" ]]; then
    echo "${base}.tar.gz"
  else
    echo "${base}-${tag}.tar.gz"
  fi
}

# ---------------------------------------------------------------------------
# Parse args
# ---------------------------------------------------------------------------
SKIP_BUILD=false
for arg in "$@"; do
  case "$arg" in
    --skip-build) SKIP_BUILD=true ;;
    *) die "Unknown argument: $arg" ;;
  esac
done

# ---------------------------------------------------------------------------
# Preflight checks
# ---------------------------------------------------------------------------
docker info > /dev/null 2>&1 || die "Docker daemon is not running or not accessible"

if [[ ! -f "$COMPOSE_BASE" ]]; then
  die "Compose file not found: $COMPOSE_BASE (run from repo root)"
fi

if [[ ! -d "$MODELS_DIR" ]]; then
  die "models/ directory not found at $(pwd)/models — mount or copy ML models before packaging"
fi

# ---------------------------------------------------------------------------
# Step 1: Clean and recreate dist/
# ---------------------------------------------------------------------------
log "Cleaning dist/ ..."
rm -rf "$DIST_DIR"
mkdir -p "$DIST_DIR/images"
mkdir -p "$DIST_DIR/config"

# ---------------------------------------------------------------------------
# Step 2: (Optional) Build Docker images
# ---------------------------------------------------------------------------
if [[ "$SKIP_BUILD" == "false" ]]; then
  log "Building Docker images (GPU compose override) ..."
  docker compose \
    -f "$COMPOSE_BASE" \
    -f "$COMPOSE_GPU" \
    build --progress=plain
else
  log "Skipping build (--skip-build)"
fi

# ---------------------------------------------------------------------------
# Step 3: Save images
# ---------------------------------------------------------------------------
save_image() {
  local img="$1"
  local filename dest tmp
  filename=$(image_to_filename "$img")
  dest="$DIST_DIR/images/$filename"
  tmp="${dest}.tmp"
  trap 'rm -f "$tmp"' ERR
  log "  Saving $img -> $dest ..."
  docker save "$img" | gzip > "$tmp"
  mv "$tmp" "$dest"
}

log "Saving built images ..."
for img in "${BUILT_IMAGES[@]}"; do
  save_image "$img"
done

log "Saving external images ..."
for img in "${EXTERNAL_IMAGES[@]}"; do
  save_image "$img"
done

# ---------------------------------------------------------------------------
# Step 4: Bundle models/
# ---------------------------------------------------------------------------
log "Bundling models/ -> $DIST_DIR/models.tar.gz ..."
tar -czf "$DIST_DIR/models.tar.gz" "$MODELS_DIR"

# ---------------------------------------------------------------------------
# Step 5: Copy compose files + .env.example
# ---------------------------------------------------------------------------
log "Copying config files ..."
sed 's|../../models|../models|g' "$COMPOSE_BASE" > "$DIST_DIR/config/docker-compose.yml"
sed 's|../../models|../models|g' "$COMPOSE_GPU"  > "$DIST_DIR/config/docker-compose.gpu.yml"
log "  Rewrote ../../models -> ../models for target deployment layout"
if [[ -f ".env.example" ]]; then
  cp ".env.example" "$DIST_DIR/config/.env.example"
else
  die ".env.example not found at repo root — bundle would be incomplete without it"
fi

# ---------------------------------------------------------------------------
# Step 6: Copy install.sh
# ---------------------------------------------------------------------------
if [[ -f "$INSTALL_SRC" ]]; then
  cp "$INSTALL_SRC" "$DIST_DIR/install.sh"
  chmod +x "$DIST_DIR/install.sh"
else
  warn "$INSTALL_SRC not found — install.sh will NOT be included in bundle (create it as Task 3)"
fi

# ---------------------------------------------------------------------------
# Step 7: Generate MANIFEST.txt
# ---------------------------------------------------------------------------
log "Generating MANIFEST.txt ..."

BUILD_DATE=$(date -u '+%Y-%m-%d %H:%M:%S UTC')
BUILD_HOST=$(hostname)
BUILD_KERNEL=$(uname -r)
DOCKER_VERSION=$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo "unknown")
MODELS_SHA=$(sha256sum "$DIST_DIR/models.tar.gz" | awk '{print $1}')
MODELS_SIZE=$(du -sh "$DIST_DIR/models.tar.gz" | awk '{print $1}')

{
  echo "========================================"
  echo "IA Text Orchestrator — Deployment Bundle"
  echo "========================================"
  echo ""
  echo "Build timestamp : $BUILD_DATE"
  echo "Build host      : $BUILD_HOST"
  echo "Kernel          : $BUILD_KERNEL"
  echo "Docker version  : $DOCKER_VERSION"
  echo ""
  echo "----------------------------------------"
  echo "Docker Images"
  echo "----------------------------------------"
} > "$DIST_DIR/MANIFEST.txt"

for img in "${BUILT_IMAGES[@]}" "${EXTERNAL_IMAGES[@]}"; do
  digest=$(docker inspect --format='{{if .RepoDigests}}{{index .RepoDigests 0}}{{end}}' "$img" 2>/dev/null || echo "")
  if [[ -z "$digest" ]]; then
    # Fall back to image ID (short 12-char) for locally-built images with no digest
    short_id=$(docker inspect --format='{{slice .Id 7 19}}' "$img" 2>/dev/null || echo "unavailable")
    echo "  $img  sha256:$short_id" >> "$DIST_DIR/MANIFEST.txt"
  else
    # Use the digest, abbreviated to 12 chars after "sha256:"
    short_digest=$(echo "$digest" | sed 's/.*sha256://' | cut -c1-12)
    echo "  $img  sha256:$short_digest" >> "$DIST_DIR/MANIFEST.txt"
  fi
done

{
  echo ""
  echo "----------------------------------------"
  echo "Models Archive"
  echo "----------------------------------------"
  echo "  models.tar.gz  sha256:$MODELS_SHA  size:$MODELS_SIZE"
  echo ""
  echo "----------------------------------------"
  echo "Installation"
  echo "----------------------------------------"
  echo "  1. Transfer the dist/ directory to the target machine"
  echo "  2. On the target machine run:"
  echo "       bash install.sh"
  echo "  3. Or manually:"
  echo "       a) Load images:  for f in images/*.tar.gz; do docker load < \"\$f\"; done"
  echo "       b) Extract models: tar -xzf models.tar.gz -C /opt/ia-text/"
  echo "       c) Copy config:  cp config/docker-compose*.yml config/.env.example /opt/ia-text/"
  echo "       d) Start stack:  docker compose -f docker-compose.yml up -d"
} >> "$DIST_DIR/MANIFEST.txt"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo "======================================"
echo "✓ Deployment bundle ready at dist/"
echo "======================================"
echo ""
echo "Next step:"
echo "  make deploy HOST=<target-ip>"
