# GLiNER Entity Extraction Worker - Python
# Multi-stage build for air-gapped deployment

# Stage 1: Builder (for any compilation needs)
FROM python:3.11-slim as builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# Stage 2: Final image
FROM python:3.11-slim

LABEL maintainer="ia-text-orchestrator"
LABEL description="GLiNER Entity Extraction Worker for NER tasks"

# ⭐ Strict Air-Gapped Mode - Set BEFORE any Python imports
ENV HF_HUB_OFFLINE=1
ENV HF_HOME=/home/app/.cache/huggingface
ENV TRANSFORMERS_OFFLINE=1
ENV HF_DATASETS_OFFLINE=1

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create app user and directories early
RUN useradd --create-home --shell /bin/bash app \
    && mkdir -p /models /home/app/.cache/huggingface

# Copy HuggingFace cache with proper structure (CRITICAL for offline mode)
# This cache contains models with the structure transformers expects
# COPY --chown=app:app models/huggingface_cache/hub /home/app/.cache/huggingface/hub

# Copy GLiNER model files
# COPY --chown=app:app models/gliner_multitask-v0.5 /models/gliner_multitask-v0.5

# Copy requirements
COPY cmd/entities-worker/requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
    && pip install --no-cache-dir -r requirements.txt

# Copy shared packages (events_python)
COPY pkg/ ./pkg/

# Copy worker code and app structure
COPY cmd/entities-worker/worker.py .
COPY cmd/entities-worker/entrypoint.sh /entrypoint.sh
COPY cmd/entities-worker/app ./app

# Fix permissions
RUN chown -R app:app /models /app /home/app && chmod +x /entrypoint.sh

# Switch to app user
USER app

# Expose metrics port
EXPOSE 8002

# Environment variables (explicitly set for clarity)
ENV PYTHONUNBUFFERED=1
ENV GLINER_MODEL_PATH=/models/gliner_multitask-v0.5

# Run entrypoint script
CMD ["/entrypoint.sh"]