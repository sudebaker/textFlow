#!/usr/bin/env bash
# deploy/package/install.sh
# Runs ON THE TARGET machine after the bundle has been transferred.
# Loads Docker images, extracts models, configures .env, creates volumes,
# starts the stack, and verifies health.
# Usage: bash install.sh
# Must be run from inside the deployment directory (where images/, models.tar.gz,
# config/, and install.sh all live).

set -euo pipefail

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
SCRIPT_NAME="install"
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# ---------------------------------------------------------------------------
# 1. check_prerequisites
# ---------------------------------------------------------------------------
check_prerequisites() {
  log "Checking prerequisites ..."

  # Docker — fatal if missing
  if ! command -v docker > /dev/null 2>&1; then
    die "docker is not installed or not on PATH"
  fi
  local docker_version
  docker_version=$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo "unknown")
  log "  docker       : $docker_version"

  # nvidia-smi — warn only
  if command -v nvidia-smi > /dev/null 2>&1; then
    local gpu_info
    gpu_info=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 || echo "unknown")
    log "  nvidia-smi   : found ($gpu_info)"
  else
    warn "nvidia-smi not found — GPU acceleration will not be available (required for production)"
  fi

  # Disk space — require at least 40 GB free
  local avail_kb
  avail_kb=$(df . | awk 'NR==2 {print $4}')
  local avail_gb=$(( avail_kb / 1024 / 1024 ))
  if (( avail_gb < 40 )); then
    die "Insufficient disk space: ${avail_gb} GB available, 40 GB required"
  fi
  log "  disk space   : ${avail_gb} GB available (>= 40 GB required)"

  log "✓ Prerequisites satisfied"
}

# ---------------------------------------------------------------------------
# 2. load_images
# ---------------------------------------------------------------------------
load_images() {
  log "Loading Docker images ..."

  if [[ ! -d "images" ]]; then
    die "images/ directory not found — bundle may be incomplete"
  fi

  local tars=()
  # Use find with -print0 / read -d '' to safely handle any filename
  while IFS= read -r -d '' tar; do
    tars+=("$tar")
  done < <(find images/ -name "*.tar.gz" -print0 2>/dev/null | sort -z)

  if [[ ${#tars[@]} -eq 0 ]]; then
    die "No .tar.gz files found in images/"
  fi

  local count=0
  for tar in "${tars[@]}"; do
    local image_name
    image_name=$(basename "$tar" .tar.gz)
    log "  Loading $image_name..."
    docker load -i "$tar" 2>&1 | grep "Loaded image" || true
    (( count++ )) || true
  done

  log "✓ Loaded $count image(s)"
  echo ""
}

# ---------------------------------------------------------------------------
# 3. extract_models
# ---------------------------------------------------------------------------
extract_models() {
  log "Extracting models ..."

  if [[ -d "models" ]]; then
    log "  models/ already exists — skipping extraction (idempotent)"
    return 0
  fi

  if [[ ! -f "models.tar.gz" ]]; then
    die "models.tar.gz not found — bundle may be incomplete"
  fi

  tar xzf models.tar.gz

  local usage
  usage=$(du -sh models/ | awk '{print $1}')
  log "  Disk usage after extract: $usage"
  log "✓ Models extracted"
}

# ---------------------------------------------------------------------------
# 4. configure_env
# ---------------------------------------------------------------------------
configure_env() {
  log "Configuring environment ..."

  if [[ -f ".env" ]]; then
    log "  .env already exists — skipping copy (idempotent)"
  else
    if [[ ! -f "config/.env.example" ]]; then
      die "config/.env.example not found — cannot create .env"
    fi
    cp config/.env.example .env
    log "  Created .env from config/.env.example"
  fi

  # Ensure air-gapped vars are set regardless of whether .env was just created
  # or pre-existing.  sed -i replaces in-place; append if the key is absent.
  _set_env_var "HF_HUB_OFFLINE" "1"
  _set_env_var "TRANSFORMERS_OFFLINE" "1"
  _set_env_var "ALLOW_REMOTE_DOWNLOAD" "false"
  _set_env_var "MODELS_PATH" "../models"

  log "✓ Air-gapped mode enabled"
}

# Helper: set KEY=VALUE in .env — replace existing line or append.
_set_env_var() {
  local key="$1"
  local value="$2"
  if grep -q "^${key}=" .env 2>/dev/null; then
    sed -i "s|^${key}=.*|${key}=${value}|" .env
  else
    echo "${key}=${value}" >> .env
  fi
}

# ---------------------------------------------------------------------------
# 5. setup_volumes
# ---------------------------------------------------------------------------
setup_volumes() {
  log "Setting up volumes and directories ..."

  # Named Docker volumes — treat "already exists" as success, die on real errors
  docker volume create redis-data 2>/dev/null || \
    docker volume inspect redis-data > /dev/null 2>&1 || \
    die "Failed to create or verify redis-data volume"
  docker volume create rabbitmq-data 2>/dev/null || \
    docker volume inspect rabbitmq-data > /dev/null 2>&1 || \
    die "Failed to create or verify rabbitmq-data volume"

  # Bind-mount directories
  mkdir -p uploads-data results-data data entities-cache
  chmod 755 uploads-data results-data data entities-cache

  log "✓ Volumes and directories ready"
}

# ---------------------------------------------------------------------------
# 6. start_services
# ---------------------------------------------------------------------------
start_services() {
  log "Starting services with Docker Compose ..."

  docker compose \
    -f config/docker-compose.yml \
    -f config/docker-compose.gpu.yml \
    up -d

  log "✓ Docker Compose started"
}

# ---------------------------------------------------------------------------
# 7. wait_for_health
# ---------------------------------------------------------------------------
wait_for_health() {
  log "Waiting for orchestrator to become healthy ..."

  local max_attempts=12
  local interval=5
  local attempt=1

  while (( attempt <= max_attempts )); do
    local response
    response=$(curl -sf http://localhost:9080/health 2>/dev/null || true)
    if echo "$response" | grep -qE '"status"\s*:\s*"(healthy|ok)"'; then
      log "✓ Orchestrator is healthy"
      return 0
    fi
    log "  Attempt $attempt/$max_attempts — not ready yet, retrying in ${interval}s ..."
    sleep "$interval"
    (( attempt++ )) || true
  done

  warn "Orchestrator did not respond healthy within $(( max_attempts * interval ))s"
  warn "The stack may still be starting up. Check with:"
  warn "  docker compose -f config/docker-compose.yml logs orchestrator"
}

# ---------------------------------------------------------------------------
# 8. print_status
# ---------------------------------------------------------------------------
print_status() {
  echo ""
  echo "======================================"
  echo "  Deployment complete"
  echo "======================================"
  echo ""
  echo "  Service              URL"
  echo "  -------------------- ----------------------------"
  echo "  Orchestrator API     http://localhost:9080"
  echo "  Orchestrator health  http://localhost:9080/health"
  echo "  Docling              http://localhost:8000"
  echo "  RabbitMQ management  http://localhost:15672"
  echo ""
  echo "Verify the stack:"
  echo "  docker compose -f config/docker-compose.yml ps"
  echo "  curl http://localhost:9080/health"
  echo ""
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
main() {
  echo ""
  echo "======================================"
  echo "  textFlow — Installer"
  echo "======================================"
  echo ""

  check_prerequisites
  load_images
  extract_models
  configure_env
  setup_volumes
  start_services
  wait_for_health
  print_status
}

main "$@"
