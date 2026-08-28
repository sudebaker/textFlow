#!/usr/bin/env bash
# deploy/package/verify-installation.sh
# Runs ON THE TARGET machine after install.sh (§26). No internet required.
# Checks: docker images present, compose valid, containers healthy,
# openapi/api smoke via fixtures, nvidia-smi if GPU.
# Usage: bash verify-installation.sh
# Exit 0 = healthy, 1 = issues detected (warns are non-fatal, errors are fatal).

set -euo pipefail

SCRIPT_NAME="verify-installation"
# shellcheck source=lib.sh
# When run from dist/ (post-transfer), lib.sh is alongside; else from repo.
for p in lib.sh deploy/package/lib.sh dist/lib.sh; do
  if [[ -f "$p" ]]; then source "$p"; break; fi
done

errors=0
check() { local d="$1" rc="$2"; if [[ "$rc" -eq 0 ]]; then log "  ✓ $d"; else warn "  ✗ $d"; errors=$((errors+1)); fi; }

log "Verifying installation ..."

# docker available
command -v docker >/dev/null 2>&1 && check "docker available" 0 || check "docker available" 1

# images present (at least orchestrator + redis + rabbitmq)
docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep -q "docker-orchestrator" \
  && check "docker-orchestrator image loaded" 0 \
  || check "docker-orchestrator image loaded" 1
docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep -q "rabbitmq" \
  && check "rabbitmq image loaded" 0 \
  || check "rabbitmq image loaded" 1
docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep -q "redis" \
  && check "redis image loaded" 0 \
  || check "redis image loaded" 1

# compose file present
cfg=""
for c in docker-compose.yml config/docker-compose.yml deploy/docker/docker-compose.yml; do
  if [[ -f "$c" ]]; then cfg="$c"; break; fi
done
[[ -n "$cfg" ]] && check "compose file present ($cfg)" 0 || check "compose file present" 1

if [[ -n "$cfg" ]] && command -v docker >/dev/null 2>&1; then
  MODELS_PATH=/tmp docker compose -f "$cfg" config >/dev/null 2>&1 \
    && check "docker compose config valid" 0 \
    || check "docker compose config valid" 1
fi

# containers healthy (at least rabbitmq, redis, docling, orchestrator if running)
if command -v docker >/dev/null 2>&1; then
  for svc in textflow-rabbitmq textflow-redis textflow-docling textflow-orchestrator; do
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^${svc}$"; then
      check "container $svc running" 0
    else
      # warn only — not fatal if stack not yet started
      warn "  - container $svc not running (is the stack up?)"
    fi
  done
fi

# openapi en destino
if curl -sf http://localhost:5001/openapi.json >/dev/null 2>&1; then
  check "docling openapi reachable" 0
else
  warn "  - docling not reachable (is the stack up?)"
fi
if curl -sf http://localhost:8080/health >/dev/null 2>&1; then
  check "orchestrator /health reachable" 0
else
  warn "  - orchestrator /health not reachable (is the stack up?)"
fi

# GPU
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi >/dev/null 2>&1 && check "nvidia-smi reachable" 0 || check "nvidia-smi reachable" 1
else
  log "  - nvidia-smi not found (CPU-only target — ok)"
fi

# summary: only hard errors fail; warns about containers not running don't fail if stack not up
if [[ "$errors" -eq 0 ]]; then
  log "Installation looks healthy."
  exit 0
else
  warn "Installation has $errors hard error(s)."
  exit 1
fi
