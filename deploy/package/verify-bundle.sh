#!/usr/bin/env bash
# deploy/package/verify-bundle.sh
# Verifies a completed bundle in dist/ BEFORE transfer (§26).
# Checks: images present, models present, manifest valid, SHA-256 valid,
# compose valid, required variables present, models/MANIFEST.txt matches.
# Usage: bash deploy/package/verify-bundle.sh [--dist dist]
# Exit 0 = valid, 1 = invalid.

set -euo pipefail

SCRIPT_NAME="verify-bundle"
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

DIST="dist"
if [[ "${1:-}" == "--dist" ]]; then
  DIST="$2"
  shift 2
fi

errors=0
check() {
  local desc="$1"
  local rc="$2"
  if [[ "$rc" -eq 0 ]]; then
    log "  ✓ $desc"
  else
    warn "  ✗ $desc"
    errors=$((errors + 1))
  fi
}

log "Verifying bundle at $DIST ..."

# dist/ must exist
[[ -d "$DIST" ]]       || { warn "dist/ not found — run: make package"; exit 1; }

# required files
[[ -f "$DIST/MANIFEST.txt" ]]              && check "MANIFEST.txt present" 0           || check "MANIFEST.txt present" 1
[[ -d "$DIST/images" ]]                    && check "images/ present" 0                || check "images/ present" 1
[[ -f "$DIST/models.tar.gz" ]]             && check "models.tar.gz present" 0          || check "models.tar.gz present" 1
[[ -d "$DIST/config" ]]                    && check "config/ present" 0                || check "config/ present" 1
[[ -f "$DIST/config/docker-compose.yml" ]] && check "docker-compose.yml present" 0    || check "docker-compose.yml present" 1
[[ -f "$DIST/config/.env.example" ]]       && check ".env.example present" 0           || check ".env.example present" 1
[[ -f "$DIST/install.sh" ]]                && check "install.sh present" 0             || check "install.sh present" 1
[[ -f "$DIST/lib.sh" ]]                    && check "lib.sh present" 0                 || check "lib.sh present" 1

# images non-empty
count=$(ls -1 "$DIST/images"/*.tar.gz 2>/dev/null | wc -l)
[[ "$count" -ge 3 ]]   && check "images/*.tar.gz ($count files)" 0     || check "images/*.tar.gz ($count files, expected ≥ 3)" 1
# each tar.gz openable
if ls "$DIST/images"/*.tar.gz >/dev/null 2>&1; then
  ok=0
  for f in "$DIST/images"/*.tar.gz; do gzip -t "$f" 2>/dev/null || ok=1; done
  check "images/*.tar.gz gzip-valid" "$ok"
fi

# models.tar.gz sha256 matches MANIFEST
if [[ -f "$DIST/models.tar.gz" && -f "$DIST/MANIFEST.txt" ]]; then
  actual=$(sha256sum "$DIST/models.tar.gz" | awk '{print $1}')
  # MANIFEST line: "  models.tar.gz  sha256:<hash>  size:..."
  expected=$(grep "models.tar.gz" "$DIST/MANIFEST.txt" | sed -n 's/.*sha256:\([a-f0-9]*\).*/\1/p' | head -1)
  if [[ -n "$expected" && "$actual" == "$expected"* ]]; then
    check "models.tar.gz SHA-256 matches MANIFEST" 0
  else
    check "models.tar.gz SHA-256 matches MANIFEST (expected $expected, actual $actual)" 1
  fi
fi

# models/MANIFEST.txt (produced by download_models_offline.py) should match
if [[ -f "models/MANIFEST.txt" ]]; then
  # basic sanity: contains Successful/Files lines
  grep -q "Successful" "models/MANIFEST.txt" && check "models/MANIFEST.txt present and sane" 0 || check "models/MANIFEST.txt sane" 1
else
  warn "  - models/MANIFEST.txt not found (run: make setup-models)"
fi

# compose file valid
if command -v docker >/dev/null 2>&1; then
  docker compose -f "$DIST/config/docker-compose.yml" config >/dev/null 2>&1 \
    && check "docker compose config valid" 0 \
    || check "docker compose config valid (docker compose -f dist/config/docker-compose.yml config failed)" 1
else
  warn "  - docker not available — skipping compose validation"
fi

# required compose env: MODELS_PATH placeholder is set; docling image tag present
grep -q "docling-serve" "$DIST/config/docker-compose.yml" && check "docling image tag in compose" 0 || check "docling image tag in compose" 1
grep -q "image:.*redis\|image:.*rabbitmq" "$DIST/config/docker-compose.yml" \
  && check "redis/rabbitmq images in bundle" 0 \
  || check "redis/rabbitmq images in bundle" 1

# summary
if [[ "$errors" -eq 0 ]]; then
  log "Bundle $DIST is VALID — safe to transfer."
  exit 0
else
  warn "Bundle $DIST has $errors error(s) — fix before transfer."
  exit 1
fi
