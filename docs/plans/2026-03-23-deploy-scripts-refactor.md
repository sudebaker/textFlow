# Deploy Scripts Refactor Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Address 6 code-quality issues identified in the senior architect review of `deploy/package/` scripts.

**Architecture:** Five targeted changes across the deploy script suite — remove a leaked dev note, extract shared logging helpers into a sourced lib, replace a fragile `sed` path rewrite with an env var, derive the Docker image list dynamically from the compose file, and add global cleanup on failure.

**Tech Stack:** Bash, Docker Compose, jq

---

## Context

All scripts live in `deploy/package/`. They are run from the **repo root** (except `install.sh`, which ships in the deployment bundle and runs on the target machine from `~/ia-text-deployment/`).

Shell safety rules in force across all scripts:
- `set -euo pipefail`
- `local` vars inside functions
- All expansions quoted
- `log`/`warn`/`die` helpers with `[scriptname]` prefix

Task 5 (SRP: extract named functions) is **deferred**. Not included here.

---

## Task 6: Remove dev note from package.sh (Trivial)

**Files:**
- Modify: `deploy/package/package.sh:162`

**Step 1: Edit the line**

Change:
```bash
warn "$INSTALL_SRC not found — install.sh will NOT be included in bundle (create it as Task 3)"
```
To:
```bash
warn "$INSTALL_SRC not found — install.sh will NOT be included in bundle"
```

**Step 2: Verify syntax**

```bash
bash -n deploy/package/package.sh
```
Expected: no output (clean parse)

**Step 3: Commit**

```bash
git add deploy/package/package.sh
git commit -m "fix(deploy): remove task-management artifact from production warn message"
```

---

## Task 1: Extract shared lib.sh (DRY)

**Files:**
- Create: `deploy/package/lib.sh`
- Modify: `deploy/package/package.sh` (remove helpers, add source)
- Modify: `deploy/package/deploy.sh` (remove helpers, add source)
- Modify: `deploy/package/install.sh` (remove helpers, add source)
- Modify: `tests/deployment/test_package.sh` (remove helpers, add source via REPO_ROOT)

**Step 1: Create lib.sh**

```bash
#!/usr/bin/env bash
# deploy/package/lib.sh
# Shared logging helpers for all deploy scripts.
# Must be sourced AFTER SCRIPT_NAME is set by the caller.

log()  { echo "[${SCRIPT_NAME}] $*"; }
warn() { echo "[${SCRIPT_NAME}] WARNING: $*" >&2; }
die()  { echo "[${SCRIPT_NAME}] ERROR: $*" >&2; exit 1; }
```

**Step 2: Update package.sh**

Replace the 3-line helpers block (lines 40–42):
```bash
log()  { echo "[package] $*"; }
warn() { echo "[package] WARNING: $*" >&2; }
die()  { echo "[package] ERROR: $*" >&2; exit 1; }
```
With:
```bash
SCRIPT_NAME="package"
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
```

**Step 3: Update deploy.sh**

Replace the 3-line helpers block (lines 18–20):
```bash
log()  { echo "[deploy] $*"; }
warn() { echo "[deploy] WARNING: $*" >&2; }
die()  { echo "[deploy] ERROR: $*" >&2; exit 1; }
```
With:
```bash
SCRIPT_NAME="deploy"
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
```

**Step 4: Update install.sh**

Replace the 3-line helpers block (lines 15–17):
```bash
log()  { echo "[install] $*"; }
warn() { echo "[install] WARNING: $*" >&2; }
die()  { echo "[install] ERROR: $*" >&2; exit 1; }
```
With:
```bash
SCRIPT_NAME="install"
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
```

**Step 5: Update test_package.sh**

`test_package.sh` lives in `tests/deployment/` — it must reach lib.sh via `$REPO_ROOT`.
Reorder the top section so REPO_ROOT is computed before the helpers, then source lib.sh:

Replace:
```bash
# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log()  { echo "[test_package] $*"; }
warn() { echo "[test_package] WARNING: $*" >&2; }
die()  { echo "[test_package] ERROR: $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Resolve repo root and cd into it
# ---------------------------------------------------------------------------
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"
log "Repo root: $REPO_ROOT"
```
With:
```bash
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
```

**Step 6: Bundle lib.sh with install.sh in package.sh**

In `package.sh`, in the "Copy install.sh" block, add a line to also copy `lib.sh`:
```bash
if [[ -f "$INSTALL_SRC" ]]; then
  cp "$INSTALL_SRC" "$DIST_DIR/install.sh"
  cp "deploy/package/lib.sh" "$DIST_DIR/lib.sh"
  chmod +x "$DIST_DIR/install.sh"
else
  warn "$INSTALL_SRC not found — install.sh will NOT be included in bundle"
fi
```

**Step 7: Verify syntax on all 5 files**

```bash
bash -n deploy/package/lib.sh
bash -n deploy/package/package.sh
bash -n deploy/package/deploy.sh
bash -n deploy/package/install.sh
bash -n tests/deployment/test_package.sh
```
Expected: no output from any command.

**Step 8: Smoke-test sourcing**

```bash
SCRIPT_NAME=test source deploy/package/lib.sh && log "lib.sh sourced OK"
```
Expected: `[test] lib.sh sourced OK`

**Step 9: Commit**

```bash
git add deploy/package/lib.sh deploy/package/package.sh deploy/package/deploy.sh \
        deploy/package/install.sh tests/deployment/test_package.sh
git commit -m "refactor(deploy): extract shared log/warn/die helpers into lib.sh"
```

---

## Task 2: Replace sed path rewrite with MODELS_PATH env var

**Files:**
- Modify: `deploy/docker/docker-compose.yml` (4 volume lines)
- Modify: `.env.example` (add MODELS_PATH)
- Modify: `deploy/package/package.sh` (remove sed, use plain cp)
- Modify: `deploy/package/install.sh` (set MODELS_PATH in configure_env)
- Modify: `tests/deployment/test_package.sh` (add assertion)

**Step 1: Update docker-compose.yml**

Change the 4 volume mount lines that reference `../../models`:
```yaml
# Line 152 — embeddings-worker
- ../../models:/models
# becomes:
- ${MODELS_PATH}:/models

# Line 192 — entities-worker
- ../../models:/models
# becomes:
- ${MODELS_PATH}:/models

# Line 193 — entities-worker huggingface cache
- ../../models/huggingface_cache:/home/app/.cache/huggingface
# becomes:
- ${MODELS_PATH}/huggingface_cache:/home/app/.cache/huggingface

# Line 322 — docling
- ../../models/docling:/models/docling
# becomes:
- ${MODELS_PATH}/docling:/models/docling
```

**Step 2: Add MODELS_PATH to .env.example**

Add to the `MODEL PATHS` section (after `BGE_MODEL_PATH` line):
```bash
# Host path to the models directory, resolved relative to the compose file.
# Dev (compose runs from deploy/docker/): ../../models
# Deployment target (set automatically by install.sh): ../models
MODELS_PATH=../../models
```

**Step 3: Remove sed rewrite from package.sh**

Replace lines 145–148:
```bash
log "Copying config files ..."
sed 's|../../models|../models|g' "$COMPOSE_BASE" > "$DIST_DIR/config/docker-compose.yml"
sed 's|../../models|../models|g' "$COMPOSE_GPU"  > "$DIST_DIR/config/docker-compose.gpu.yml"
log "  Rewrote ../../models -> ../models for target deployment layout"
```
With:
```bash
log "Copying config files ..."
cp "$COMPOSE_BASE" "$DIST_DIR/config/docker-compose.yml"
cp "$COMPOSE_GPU"  "$DIST_DIR/config/docker-compose.gpu.yml"
```

**Step 4: Set MODELS_PATH in install.sh configure_env()**

In `configure_env()`, after the existing `_set_env_var "ALLOW_REMOTE_DOWNLOAD" "false"` line, add:
```bash
_set_env_var "MODELS_PATH" "../models"
```

**Step 5: Add test assertion in test_package.sh**

After the config files check block, add:
```bash
# Verify no hardcoded ../../models remain in bundled compose (Task 2 regression guard)
if grep -q '../../models' dist/config/docker-compose.yml; then
  die "dist/config/docker-compose.yml still contains hardcoded ../../models — sed rewrite regressed"
fi
log "  [OK] no hardcoded ../../models in dist/config/docker-compose.yml"
```

**Step 6: Verify compose renders correctly**

```bash
MODELS_PATH=../../models docker compose -f deploy/docker/docker-compose.yml config | grep -A1 "volumes:" | grep models
```
Expected: lines showing `/models` paths with the resolved value.

Also check syntax:
```bash
bash -n deploy/package/package.sh
bash -n deploy/package/install.sh
bash -n tests/deployment/test_package.sh
```

**Step 7: Commit**

```bash
git add deploy/docker/docker-compose.yml .env.example \
        deploy/package/package.sh deploy/package/install.sh \
        tests/deployment/test_package.sh
git commit -m "fix(deploy): replace sed path rewrite with MODELS_PATH env var"
```

---

## Task 3: Derive BUILT_IMAGES dynamically from compose

**Files:**
- Modify: `deploy/package/package.sh` (replace hardcoded array + add jq preflight)

**Step 1: Add jq to preflight checks**

In the preflight section (after the `docker info` check), add:
```bash
command -v jq > /dev/null 2>&1 || die "jq is not installed. Install: sudo apt-get install jq"
```

**Step 2: Replace the hardcoded BUILT_IMAGES array**

Replace lines 18–28:
```bash
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
```
With:
```bash
# Built images — derived dynamically from compose (project name = "docker")
# Populated after preflight confirms jq is available.
BUILT_IMAGES=()
```

Then, after the preflight section (after the models/ check), add the population step:
```bash
# ---------------------------------------------------------------------------
# Derive BUILT_IMAGES from compose
# ---------------------------------------------------------------------------
log "Deriving built images from $COMPOSE_BASE ..."
mapfile -t BUILT_IMAGES < <(
  docker compose -f "$COMPOSE_BASE" config --format json \
    | jq -r '.services | to_entries[]
             | select(.value.build != null)
             | "docker-\(.key):latest"'
)
[[ ${#BUILT_IMAGES[@]} -gt 0 ]] || die "No buildable services found in $COMPOSE_BASE — check compose file"
log "  Found ${#BUILT_IMAGES[@]} built image(s): ${BUILT_IMAGES[*]}"
```

**Step 3: Verify**

```bash
bash -n deploy/package/package.sh
```

Then dry-run the derivation manually:
```bash
docker compose -f deploy/docker/docker-compose.yml config --format json \
  | jq -r '.services | to_entries[] | select(.value.build != null) | "docker-\(.key):latest"'
```
Expected: 8 lines matching the old hardcoded list (order may differ, that's fine).

**Step 4: Commit**

```bash
git add deploy/package/package.sh
git commit -m "refactor(deploy): derive BUILT_IMAGES dynamically from compose config"
```

---

## Task 4: Add trap EXIT cleanup to package.sh

**Files:**
- Modify: `deploy/package/package.sh`

**Step 1: Add cleanup trap**

Immediately after `set -euo pipefail` (line 7), add:
```bash
_cleanup() {
  local exit_code=$?
  if [[ $exit_code -ne 0 ]]; then
    warn "Interrupted or failed (exit ${exit_code}) — removing partial dist/ to prevent stale bundle"
    rm -rf "$DIST_DIR"
  fi
}
trap _cleanup EXIT
```

Note: the existing per-image `trap 'rm -f "$tmp"' ERR` inside `save_image()` is kept — it handles individual temp files at a finer grain. The new global trap handles the dist/ directory as a whole.

**Step 2: Verify syntax**

```bash
bash -n deploy/package/package.sh
```
Expected: no output.

**Step 3: Commit**

```bash
git add deploy/package/package.sh
git commit -m "fix(deploy): add trap EXIT to clean partial dist/ on failure or interrupt"
```
