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
DEBERTA_PATH="/models/deberta-v3-small"
echo "   Checking DeBERTa backbone at: $DEBERTA_PATH"

if [ ! -f "$DEBERTA_PATH/config.json" ]; then
    echo "❌ ERROR: DeBERTa backbone config not found at $DEBERTA_PATH/config.json"
    echo "   This is required for GLiNER to load. DeBERTa must be pre-downloaded at build time on the host."
    exit 1
fi

echo "   ✓ DeBERTa backbone found at $DEBERTA_PATH"

echo ""
echo "================================================================================"
echo "✅ Pre-flight checks passed"
echo "================================================================================"
echo ""

# Start the worker
echo "🚀 Starting worker..."
exec python worker.py
