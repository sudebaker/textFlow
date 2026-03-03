# GLiNER Offline Mode - Complete Guide

## Problem Description

GLiNER is a Named Entity Recognition (NER) model that requires a DeBERTa backbone tokenizer. When deploying GLiNER in air-gapped environments (no internet access), it fails with:

```
OSError: We couldn't connect to 'https://huggingface.co' to load the files, and couldn't find them in the cached files.
```

Even when:
- Environment variables `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1` are set
- DeBERTa model files are cached locally
- `local_files_only=True` is passed to `GLiNER.from_pretrained()`
- Monkey-patching attempts are made to force offline mode

### Root Causes

The issue has **two distinct root causes** that both must be addressed:

#### Root Cause #1: HF_HUB_OFFLINE=1 Breaks GLiNER Internally

When `HF_HUB_OFFLINE=1` is set globally, GLiNER internally calls `model_info()` from the `huggingface_hub` library, which crashes because it cannot reach the Hub and has no fallback.

**Stack trace signature:**
```
huggingface_hub.errors.LocalEntryNotFoundError: Cannot find the requested files in the disk cache and outgoing traffic has been disabled.
```

The error occurs **inside GLiNER's own code**, not in transformers. Setting `HF_HUB_OFFLINE=1` globally prevents the entire application from working.

**Solution:** Do NOT set `HF_HUB_OFFLINE=1` or `TRANSFORMERS_OFFLINE=1` as global environment variables for the GLiNER service.

#### Root Cause #2: GLiNER Config References Hub Model Name

GLiNER's `gliner_config.json` contains:
```json
"model_name": "microsoft/deberta-v3-large"
```

When GLiNER loads the tokenizer, it uses this value and calls:
```python
AutoTokenizer.from_pretrained(config.model_name, cache_dir=cache_dir)
```

Even though `cache_dir` is provided and `local_files_only=True` might be set, transformers will still attempt to verify the model online first. The model name is treated as a Hub identifier, not a local path.

**Solution:** Change `model_name` in `gliner_config.json` to an **absolute local path** where DeBERTa files actually exist in the container.

## Complete Solution

### Step 1: Modify GLiNER Config

Edit `models/gliner_model/gliner_config.json` and change the `model_name` field:

**Before:**
```json
"model_name": "microsoft/deberta-v3-large"
```

**After:**
```json
"model_name": "/models/deberta-v3-large"
```

This tells GLiNER to look for the tokenizer files at an absolute path inside the container, not on HuggingFace Hub.

### Step 2: Prepare DeBERTa Files in Container

The Dockerfile must ensure DeBERTa files exist at `/models/deberta-v3-large` with **actual files** (not symlinks or cache pointers).

**Key principle:** GLiNER needs a **flat directory** with real tokenizer files, not the HuggingFace Hub cache structure (which uses hash-based snapshot directories).

#### Option A: Copy DeBERTa from HuggingFace Cache (Recommended for Build Environments)

1. Download DeBERTa normally to `/home/app/.cache/huggingface` during the Dockerfile build
2. Copy the tokenizer files from the HF cache snapshot to a flat directory

**Dockerfile example:**
```dockerfile
# Download DeBERTa to HF cache (with internet access during build)
ENV HF_HOME=/home/app/.cache/huggingface
COPY cmd/entities-worker/download_deberta_backbone.py .
RUN python download_deberta_backbone.py

# Copy DeBERTa from HF cache snapshot to flat directory
RUN mkdir -p /models/deberta-v3-large && \
    cp -r /home/app/.cache/huggingface/models--microsoft--deberta-v3-large/snapshots/*/. /models/deberta-v3-large/

# Clean up the HF cache to save space (optional)
RUN rm -rf /home/app/.cache/huggingface
```

#### Option B: Direct Download to Flat Path

Modify `download_deberta_backbone.py` to also copy files to the flat directory:

```python
def copy_to_flat_path(cache_dir, model_id, target_path):
    """Copy DeBERTa from HF cache snapshot to a flat directory."""
    import os
    import shutil
    
    # Find the snapshot directory
    model_safe_name = "models--" + model_id.replace("/", "--")
    snapshot_dir = os.path.join(cache_dir, model_safe_name, "snapshots")
    
    if not os.path.exists(snapshot_dir):
        print(f"ERROR: Snapshot directory not found: {snapshot_dir}")
        return False
    
    snapshot_subdirs = os.listdir(snapshot_dir)
    if not snapshot_subdirs:
        print(f"ERROR: No snapshot found in {snapshot_dir}")
        return False
    
    source_path = os.path.join(snapshot_dir, snapshot_subdirs[0])
    
    # Create target directory and copy files
    os.makedirs(target_path, exist_ok=True)
    shutil.copytree(source_path, target_path, dirs_exist_ok=True)
    
    print(f"✓ Copied DeBERTa from {source_path} to {target_path}")
    return True
```

### Step 3: Do NOT Set HF_HUB_OFFLINE Globally

**Remove from Dockerfile:**
```dockerfile
# DON'T DO THIS:
ENV HF_HUB_OFFLINE=1
ENV TRANSFORMERS_OFFLINE=1

# These break GLiNER internally
```

**Remove from docker-compose.yml environment section:**
```yaml
# DON'T DO THIS for entities-worker:
environment:
  - HF_HUB_OFFLINE=1
  - TRANSFORMERS_OFFLINE=1
```

### Step 4: Configure Environment for Offline Loading

**What TO do instead:**

In `worker.py`, set environment variables BEFORE any GLiNER/transformers imports:

```python
import os

# Configure cache paths (but NOT offline mode globally)
os.environ["HF_HOME"] = "/home/app/.cache/huggingface"
os.environ["TRANSFORMERS_CACHE"] = "/home/app/.cache/huggingface"
os.environ["HF_DATASETS_CACHE"] = "/home/app/.cache/huggingface"

# DON'T set HF_HUB_OFFLINE=1 or TRANSFORMERS_OFFLINE=1 here
```

Then in the model loading code:

```python
from gliner import GLiNER

model = GLiNER.from_pretrained(
    "/models/gliner_model",
    local_files_only=True  # This is enough - no need for HF_HUB_OFFLINE
)
```

### Step 5: Remove Monkey-Patching Attempts

Delete any monkey-patch code that tries to force `local_files_only=True` globally. Once the config and paths are correct, monkey-patching is unnecessary and can cause other issues.

## Verification Checklist

Before deployment, verify:

- [ ] `models/gliner_model/gliner_config.json` has `"model_name": "/models/deberta-v3-large"`
- [ ] `/models/deberta-v3-large/` contains tokenizer files (tokenizer.json, config.json, tokenizer_config.json, etc.)
- [ ] Files are **real files**, not symlinks
- [ ] Dockerfile does NOT set `HF_HUB_OFFLINE=1` as ENV variable
- [ ] docker-compose.yml does NOT set `HF_HUB_OFFLINE=1` for entities-worker
- [ ] worker.py sets `HF_HOME` and `TRANSFORMERS_CACHE` but NOT `HF_HUB_OFFLINE`
- [ ] `GLiNER.from_pretrained()` is called with `local_files_only=True`

## Testing in Air-Gapped Environment

```bash
# Remove network access
docker run --network=none ia-text-entities-worker

# Expected: Worker starts and loads GLiNER successfully
# Log should show: "✅ GLiNER Model Loaded Successfully"
```

If it fails with a connection error, check:
1. Is `gliner_config.json` pointing to `/models/deberta-v3-large`?
2. Do the files exist in that path? `ls /models/deberta-v3-large/`
3. Is `HF_HUB_OFFLINE=1` still being set somewhere?

## Reference: Working Implementation

See `ocugraphrag` project for a complete working example:
- `/path/to/ocugraphrag/glinear_ner_service/main.py` — Correct loading pattern
- `/path/to/ocugraphrag/fix_glinear_airgap.sh` — Symlink/cache management script
- `/path/to/ocugraphrag/docs/TROUBLESHOOTING.md` — Original source of this solution

## Common Mistakes

| Mistake | Problem | Solution |
|---------|---------|----------|
| Setting `HF_HUB_OFFLINE=1` globally | GLiNER crashes internally in `model_info()` | Don't set it for GLiNER services |
| `model_name` is still a Hub identifier | transformers tries to download from Hub | Change to absolute local path `/models/...` |
| DeBERTa files are symlinks | Symlinks break in some environments | Copy actual files, not symlinks |
| Files in HF cache snapshot structure | GLiNER can't find them with direct path | Copy to flat directory `/models/deberta-v3-large/` |
| Only setting `local_files_only=True` without config change | transformers still tries Hub first | Must change config `model_name` to local path |
| Monkey-patching transformers | Patch doesn't apply due to module caching | Not needed if config/paths are correct |

## Why This Works

1. **Direct path in config** → transformers/GLiNER doesn't attempt a Hub lookup
2. **Flat file structure** → All files are where GLiNER expects them
3. **No global offline flags** → GLiNER can still call `model_info()` internally without crashing
4. **`local_files_only=True` at load time** → Additional safety: if any code path tries remote access, it will fail gracefully instead of hanging

The combination of these three elements (correct config, correct file location, correct parameters) is what makes offline GLiNER work reliably.
