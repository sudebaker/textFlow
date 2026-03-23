#!/usr/bin/env bash
# tests/deployment/test_package.sh
# Integration test: validates the output structure produced by deploy/package/package.sh --skip-build
# Usage: bash tests/deployment/test_package.sh
# Must be run from anywhere; resolves repo root automatically.

set -euo pipefail

# ---------------------------------------------------------------------------
# Resolve repo root and cd into it
# ---------------------------------------------------------------------------
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

# ---------------------------------------------------------------------------
# Helpers (sourced from deploy/package/lib.sh)
# ---------------------------------------------------------------------------
SCRIPT_NAME="test_package"
# shellcheck source=deploy/package/lib.sh
source "deploy/package/lib.sh"

log "Repo root: $REPO_ROOT"

# ---------------------------------------------------------------------------
# Preflight: check Docker daemon (package.sh requires it)
# ---------------------------------------------------------------------------
if ! docker info > /dev/null 2>&1; then
  warn "Docker daemon is not running or not accessible — skipping test"
  exit 75
fi

# ---------------------------------------------------------------------------
# Preflight: check models/ exists; if not, skip and warn
# ---------------------------------------------------------------------------
if [[ ! -d "models" ]]; then
  warn "models/ directory not found — skipping full package.sh run"
  warn "Mount or copy ML models into models/ before running this test"
  exit 75
fi

# ---------------------------------------------------------------------------
# Run package.sh --skip-build
# ---------------------------------------------------------------------------
log "Running deploy/package/package.sh --skip-build ..."
bash deploy/package/package.sh --skip-build
log "package.sh completed."

# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------
check_dir() {
  local path="$1"
  if [[ ! -d "$path" ]]; then
    die "Expected directory missing: $path"
  fi
  log "  [OK] directory exists: $path"
}

check_file() {
  local path="$1"
  if [[ ! -f "$path" ]]; then
    die "Expected file missing: $path"
  fi
  log "  [OK] file exists: $path"
}

check_nonempty() {
  local path="$1"
  if [[ ! -s "$path" ]]; then
    die "Expected non-empty file is empty or missing: $path"
  fi
  log "  [OK] file is non-empty: $path"
}

check_tar() {
  local path="$1"
  local rc=0
  # Run tar in a subshell so that `head -5` closing the pipe (SIGPIPE / exit 141)
  # does not propagate through set -o pipefail in the outer shell.
  # We only care that tar itself can open and decompress the archive; 141 means
  # head terminated early which is fine for large images.
  (tar tzf "$path" | head -5 > /dev/null) 2>/dev/null || rc=$?
  if [[ "$rc" -ne 0 && "$rc" -ne 141 ]]; then
    die "Cannot list tar archive (corrupt or invalid): $path"
  fi
  log "  [OK] tar archive is readable: $path"
}

# ---------------------------------------------------------------------------
# 1. Top-level dist/ structure
# ---------------------------------------------------------------------------
log "--- Checking dist/ structure ---"
check_dir  "dist/images"
check_dir  "dist/config"
check_file "dist/models.tar.gz"
check_file "dist/MANIFEST.txt"
check_nonempty "dist/install.sh"
[[ -x "dist/install.sh" ]] || die "dist/install.sh is not executable"
log "  [OK] dist/install.sh is executable"
check_file "dist/lib.sh"

# ---------------------------------------------------------------------------
# 2. models.tar.gz is a readable archive
# ---------------------------------------------------------------------------
log "--- Checking dist/models.tar.gz ---"
check_tar "dist/models.tar.gz"

# ---------------------------------------------------------------------------
# 3. Config files
# ---------------------------------------------------------------------------
log "--- Checking dist/config/ files ---"
check_file "dist/config/docker-compose.yml"
check_file "dist/config/docker-compose.gpu.yml"
check_file "dist/config/.env.example"

# ---------------------------------------------------------------------------
# 4. MANIFEST.txt is non-empty
# ---------------------------------------------------------------------------
log "--- Checking dist/MANIFEST.txt ---"
check_nonempty "dist/MANIFEST.txt"

# ---------------------------------------------------------------------------
# 5. Validate any images/*.tar.gz (skip gracefully if none exist)
# ---------------------------------------------------------------------------
log "--- Checking dist/images/ ---"
if compgen -G "dist/images/*.tar.gz" > /dev/null 2>&1; then
  local_image_count=0
  for img_file in dist/images/*.tar.gz; do
    check_tar "$img_file"
    local_image_count=$(( local_image_count + 1 ))
  done
  log "  [OK] validated $local_image_count image archive(s)"
else
  warn "No image archives found in dist/images/ — Docker images may not have been saved"
  warn "This is acceptable when running without pre-built images"
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
log "All checks passed"
