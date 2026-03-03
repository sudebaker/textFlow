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

# Environment setup
ENV HF_HOME=/home/app/.cache/huggingface
ENV PYTHONUNBUFFERED=1

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create app user and directories early
RUN useradd --create-home --shell /bin/bash app \
    && mkdir -p /models /home/app/.cache/huggingface

# Copy requirements
COPY cmd/entities-worker/requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
    && pip install --no-cache-dir -r requirements.txt

# Copy DeBERTa download script
COPY cmd/entities-worker/download_deberta_backbone.py .

# Download DeBERTa backbone (needed for GLiNER offline mode)
# This runs as root to ensure proper cache creation
# The script will:
# 1. Download DeBERTa to /home/app/.cache/huggingface (HF cache format)
# 2. Copy DeBERTa files to /models/deberta-v3-large (flat path for GLiNER)
RUN python download_deberta_backbone.py

# Copy GLiNER model files
COPY --chown=app:app models/gliner_model/ /models/gliner_model

# Copy shared packages (events_python)
COPY pkg/ ./pkg/

# Copy worker code, sliding window, and app structure
COPY cmd/entities-worker/worker.py .
COPY cmd/entities-worker/sliding_window.py .
COPY cmd/entities-worker/entrypoint.sh /entrypoint.sh
COPY cmd/entities-worker/app ./app

# Fix permissions
RUN chown -R app:app /models /app /home/app && chmod +x /entrypoint.sh

# ⚠️  IMPORTANT: Do NOT set HF_HUB_OFFLINE=1 or TRANSFORMERS_OFFLINE=1 here
# These break GLiNER internally (it calls model_info() which fails without Hub access)
# Instead, we use local_files_only=True at load time + correct paths in config

# Switch to app user
USER app

# Expose metrics port
EXPOSE 8002

# Run entrypoint script
CMD ["/entrypoint.sh"]
