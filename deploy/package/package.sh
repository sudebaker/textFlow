#!/usr/bin/env bash
# deploy/package/package.sh
# Produces a complete deployable bundle in dist/
# Usage: bash deploy/package/package.sh [--skip-build]
# Must be run from the repository root.

set -euo pipefail

# DIST_DIR must be defined before the trap so _cleanup can reference it safely
# under set -u even if the script exits before reaching the Config section.
DIST_DIR="dist"

_cleanup() {
  local exit_code=$?
  if [[ $exit_code -ne 0 ]]; then
    if declare -f warn > /dev/null 2>&1; then
      warn "Interrupted or failed (exit ${exit_code}) — removing partial dist/ to prevent stale bundle"
    else
      echo "[package] WARNING: Interrupted or failed (exit ${exit_code}) — removing partial dist/ to prevent stale bundle" >&2
    fi
    rm -rf "$DIST_DIR"
  fi
}
trap _cleanup EXIT

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DIST_DIR="dist"  # canonical assignment; duplicated above solely for the trap guard
COMPOSE_BASE="deploy/docker/docker-compose.yml"
COMPOSE_GPU="deploy/docker/docker-compose.gpu.yml"
MODELS_DIR="models"
INSTALL_SRC="deploy/package/install.sh"

# Built images — derived dynamically from compose after preflight (jq required)
# Project name = "docker" (compose files live in deploy/docker/)
BUILT_IMAGES=()

# External / pulled images — derived from $COMPOSE_BASE when possible; kept here
# as fallback so `make package` never silently bundles a wrong tag. The preflight
# step "Derive EXTERNAL_IMAGES from compose" (below) overwrites this list when
# jq + compose are available. Never edit just this list without checking
# deploy/docker/docker-compose.yml (rabbitmq, redis, docling-serve tags).
EXTERNAL_IMAGES=(
  "rabbitmq:3.13-management"
  "redis:7-alpine"
  "quay.io/docling-project/docling-serve:latest"
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
SCRIPT_NAME="package"
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

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
command -v jq > /dev/null 2>&1 || die "jq is not installed. Install: sudo apt-get install jq"

if [[ ! -f "$COMPOSE_BASE" ]]; then
  die "Compose file not found: $COMPOSE_BASE (run from repo root)"
fi

if [[ ! -d "$MODELS_DIR" ]]; then
  die "models/ directory not found at $(pwd)/models — mount or copy ML models before packaging"
fi

# ---------------------------------------------------------------------------
# Derive BUILT_IMAGES from compose
# ---------------------------------------------------------------------------
log "Deriving built images from $COMPOSE_BASE ..."
# MODELS_PATH=/tmp: dummy value to satisfy the :? required-variable guard in
# docker-compose.yml during config parsing. We only need the service structure
# (which services have build: keys), not the actual bind-mount paths.
_compose_json=$(MODELS_PATH=/tmp docker compose -f "$COMPOSE_BASE" config --format json) \
  || die "docker compose config failed — check $COMPOSE_BASE and ensure Docker daemon is running"
mapfile -t BUILT_IMAGES < <(
  jq -r '.services | to_entries[]
         | select(.value.build != null)
         | "docker-\(.key):latest"' <<< "$_compose_json"
)
[[ ${#BUILT_IMAGES[@]} -gt 0 ]] || die "No buildable services found in $COMPOSE_BASE — check compose file"
log "  Found ${#BUILT_IMAGES[@]} built image(s): ${BUILT_IMAGES[*]}"

# Derive EXTERNAL_IMAGES from compose `image:` entries that are NOT built
# (single source of truth — prevents rabbitmq:3.12 vs 3.13 drift).
if command -v jq > /dev/null 2>&1 && [[ -n "$_compose_json" ]]; then
  mapfile -t _derived_external < <(
    jq -r '.services | to_entries[]
           | select(.value.build == null and .value.image != null)
           | .value.image' <<< "$_compose_json" \
    | grep -v '^docker-'
  )
  if [[ ${#_derived_external[@]} -gt 0 ]]; then
    EXTERNAL_IMAGES=("${_derived_external[@]}")
    log "  Derived external image(s) from compose: ${EXTERNAL_IMAGES[*]}"
  fi
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
cp "$COMPOSE_BASE" "$DIST_DIR/config/docker-compose.yml"
cp "$COMPOSE_GPU"  "$DIST_DIR/config/docker-compose.gpu.yml"
if [[ -f ".env.example" ]]; then
  cp ".env.example" "$DIST_DIR/config/.env.example"
else
  die ".env.example not found at repo root — bundle would be incomplete without it"
fi

# ---------------------------------------------------------------------------
# Step 6: Copy install.sh
# ---------------------------------------------------------------------------
# Always bundle lib.sh (install.sh/verify scripts source it)
cp "deploy/package/lib.sh" "$DIST_DIR/lib.sh"
for vf in verify-bundle.sh verify-installation.sh; do
  if [[ -f "deploy/package/$vf" ]]; then
    cp "deploy/package/$vf" "$DIST_DIR/$vf"
    chmod +x "$DIST_DIR/$vf"
  fi
done

if [[ -f "$INSTALL_SRC" ]]; then
  cp "$INSTALL_SRC" "$DIST_DIR/install.sh"
  chmod +x "$DIST_DIR/install.sh"
else
  warn "$INSTALL_SRC not found — install.sh will NOT be included in bundle"
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
  echo "textFlow — Deployment Bundle"
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
