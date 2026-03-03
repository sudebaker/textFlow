#!/bin/bash
set -e

echo "================================================================================"
echo "🚀 GLiNER Entities Worker - Starting"
echo "================================================================================"

# Verify configuration
echo "🔍 Verifying configuration..."
echo "   HF_HUB_OFFLINE: ${HF_HUB_OFFLINE}"
echo "   TRANSFORMERS_OFFLINE: ${TRANSFORMERS_OFFLINE}"
echo "   HF_HOME: ${HF_HOME}"
echo "   GLINER_MODEL_PATH: ${GLINER_MODEL_PATH}"

# Verify GLiNER model files exist
GLINER_MODEL="${GLINER_MODEL_PATH:-/models/gliner_model}"
if [ ! -f "$GLINER_MODEL/gliner_config.json" ] && [ ! -f "$GLINER_MODEL/config.json" ]; then
    echo "❌ ERROR: GLiNER model config files not found!"
    echo "   Expected: $GLINER_MODEL/gliner_config.json or $GLINER_MODEL/config.json"
    echo "   Available files:"
    ls -la "$GLINER_MODEL/" 2>/dev/null || echo "   (directory not found)"
    exit 1
fi

if [ ! -f "$GLINER_MODEL/pytorch_model.bin" ]; then
    echo "❌ ERROR: GLiNER model weights not found!"
    echo "   Expected: $GLINER_MODEL/pytorch_model.bin"
    exit 1
fi

echo "   ✓ GLiNER model files present at $GLINER_MODEL"

# Verify DeBERTa backbone is available
HF_CACHE="${HF_HOME:-$HOME/.cache/huggingface}"
echo "   Checking DeBERTa backbone in: $HF_CACHE"

if find "$HF_CACHE" -name "config.json" -path "*/deberta-v3-large/*" >/dev/null 2>&1; then
    echo "   ✓ DeBERTa backbone found in HuggingFace cache"
else
    echo "⚠️  WARNING: DeBERTa backbone not found in HuggingFace cache"
    echo "   GLiNER may fail to load. Attempting to download..."
    
    # Try to download DeBERTa if HF_HUB_OFFLINE is not set to 1
    if [ "$HF_HUB_OFFLINE" != "1" ]; then
        echo "   Downloading DeBERTa v3 Large..."
        python3 -c "
import os
os.environ['HF_HUB_OFFLINE'] = '0'
os.environ['TRANSFORMERS_OFFLINE'] = '0'
from transformers import AutoModel
try:
    AutoModel.from_pretrained('microsoft/deberta-v3-large', cache_dir='$HF_CACHE')
    print('✓ DeBERTa download successful')
except Exception as e:
    print(f'✗ DeBERTa download failed: {e}')
    exit(1)
" || exit 1
    else
        echo "❌ ERROR: HF_HUB_OFFLINE=1 but DeBERTa not cached!"
        exit 1
    fi
fi

echo ""
echo "================================================================================"
echo "✅ Pre-flight checks passed"
echo "================================================================================"
echo ""

# Start the worker
echo "🚀 Starting worker..."
exec python worker.py
