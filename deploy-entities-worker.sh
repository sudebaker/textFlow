#!/bin/bash
# Deployment script for entities-worker with offline GLiNER support

set -e

echo "================================================================================"
echo "🚀 Deploying Entities Worker with Offline GLiNER Support"
echo "================================================================================"

COMPOSE_FILE="deploy/docker/docker-compose.yml"

# Check if models are downloaded
echo "📦 Checking models..."
if [ ! -d "models/huggingface_cache/hub/models--microsoft--deberta-v3-small" ]; then
    echo "❌ ERROR: DeBERTa model not found in cache"
    echo "   Run: python3 deploy/docker/download_models_offline.py"
    exit 1
fi

if [ ! -d "models/gliner-small-v2.1" ]; then
    echo "❌ ERROR: GLiNER model not found"
    exit 1
fi

echo "✅ Models found:"
echo "   - GLiNER: models/gliner-small-v2.1"
echo "   - DeBERTa: models/huggingface_cache/hub/models--microsoft--deberta-v3-small"

# Stop current container
echo ""
echo "🛑 Stopping current entities-worker..."
docker compose -f "$COMPOSE_FILE" stop entities-worker 2>/dev/null || true
docker compose -f "$COMPOSE_FILE" rm -f entities-worker 2>/dev/null || true

# Build new image
echo ""
echo "🔨 Building new entities-worker image..."
docker compose -f "$COMPOSE_FILE" build entities-worker

# Start new container
echo ""
echo "🚀 Starting entities-worker..."
docker compose -f "$COMPOSE_FILE" up -d entities-worker

# Wait for startup
echo ""
echo "⏳ Waiting for startup (10 seconds)..."
sleep 10

# Check status
echo ""
echo "🔍 Checking status..."
docker ps --filter "name=entities-worker" --format "table {{.Names}}\t{{.Status}}"

# Show logs
echo ""
echo "================================================================================"
echo "📋 Container logs (last 50 lines):"
echo "================================================================================"
docker logs --tail 50 ia-text-entities-worker

echo ""
echo "================================================================================"
echo "✅ Deployment complete"
echo "================================================================================"
echo ""
echo "Monitor logs with: docker logs -f ia-text-entities-worker"
echo "Check status with: docker ps | grep entities-worker"
echo ""
