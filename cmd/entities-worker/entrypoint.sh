#!/bin/bash
set -e

echo "================================================================================"
echo "🚀 GLiNER Entities Worker - Starting in Offline Mode"
echo "================================================================================"

# Verify offline environment variables
echo "🔍 Verifying offline configuration..."
echo "   HF_HUB_OFFLINE: ${HF_HUB_OFFLINE}"
echo "   TRANSFORMERS_OFFLINE: ${TRANSFORMERS_OFFLINE}"
echo "   HF_HOME: ${HF_HOME}"

# Verify HuggingFace cache exists
if [ ! -d "${HF_HOME}/hub" ]; then
    echo "❌ ERROR: HuggingFace cache not found!"
    echo "   Expected: ${HF_HOME}/hub/"
    echo "   This is CRITICAL for offline mode"
    exit 1
fi
echo "   ✓ HuggingFace cache directory exists"

# Verify DeBERTa backbone in cache
DEBERTA_CACHE="${HF_HOME}/hub/models--microsoft--deberta-v3-small"
if [ ! -d "$DEBERTA_CACHE" ]; then
    echo "❌ ERROR: DeBERTa backbone not found in cache!"
    echo "   Expected: $DEBERTA_CACHE"
    echo "   GLiNER requires this model to function"
    exit 1
fi
echo "   ✓ DeBERTa backbone found in cache"

# Verify GLiNER model files
GLINER_MODEL="/models/gliner-small-v2.1"
if [ ! -f "$GLINER_MODEL/gliner_config.json" ]; then
    echo "❌ ERROR: GLiNER model files not found!"
    echo "   Expected: $GLINER_MODEL/gliner_config.json"
    exit 1
fi
echo "   ✓ GLiNER model files present"

# List cache contents for debugging
echo ""
echo "📋 Cache contents:"
ls -la "${HF_HOME}/hub/" | head -10

echo ""
echo "================================================================================"
echo "✅ Pre-flight checks passed"
echo "================================================================================"
echo ""

# Start the worker
echo "🚀 Starting worker..."
exec python worker.py
