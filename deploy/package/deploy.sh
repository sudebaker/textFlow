#!/usr/bin/env bash
# deploy/package/deploy.sh
# Transfers the deployment bundle (dist/) to a target machine via rsync.
# Usage: bash deploy/package/deploy.sh <user@host|host>
# Must be run from the repository root.

set -euo pipefail

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DIST_DIR="dist"
REMOTE_DIR="~/ia-text-deployment/"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log()  { echo "[deploy] $*"; }
warn() { echo "[deploy] WARNING: $*" >&2; }
die()  { echo "[deploy] ERROR: $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------
usage() {
  echo "Usage: bash deploy/package/deploy.sh <user@host|host>"
  echo ""
  echo "Transfers dist/ to <host>:~/ia-text-deployment/ using rsync."
  echo ""
  echo "Examples:"
  echo "  bash deploy/package/deploy.sh 10.0.0.5"
  echo "  bash deploy/package/deploy.sh admin@10.0.0.5"
  exit 1
}

# ---------------------------------------------------------------------------
# Parse args
# ---------------------------------------------------------------------------
if [[ $# -lt 1 ]]; then
  die "Missing required argument: <host>"$'\n'"$(usage 2>&1 || true)"
fi

TARGET="$1"

# ---------------------------------------------------------------------------
# Preflight checks
# ---------------------------------------------------------------------------

# rsync must be installed
command -v rsync > /dev/null 2>&1 || die "rsync is not installed. Install it with: sudo apt-get install rsync"

# dist/ must contain the expected artifacts
if [[ ! -d "${DIST_DIR}/images" ]] || [[ ! -f "${DIST_DIR}/MANIFEST.txt" ]]; then
  die "Bundle not found (missing ${DIST_DIR}/images/ or ${DIST_DIR}/MANIFEST.txt). Run 'make package' first."
fi

# Extract just the host part (strip user@ prefix if present) for display
HOST_ONLY="${TARGET##*@}"

# ---------------------------------------------------------------------------
# Transfer
# ---------------------------------------------------------------------------
echo ""
echo "======================================"
log "Transferring bundle to ${HOST_ONLY}..."
echo "======================================"
echo ""

rsync -avz --progress --delete-after "${DIST_DIR}/" "${TARGET}:${REMOTE_DIR}"

echo ""
log "✓ Transfer complete."
echo ""
echo "======================================"
log "Next steps on target machine:"
echo "======================================"
echo ""
echo "  ssh ${TARGET}"
echo "  bash ~/ia-text-deployment/install.sh"
echo ""
