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

# ⭐ Para producción air-gapped: descomentar estas líneas
ENV HF_HUB_OFFLINE=1
ENV HF_HOME=/models

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements
COPY cmd/entities-worker/requirements.txt .

# Install PyTorch CPU-only version
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install torch --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir -r requirements.txt

# Copy shared packages (events_python)
COPY pkg/ ./pkg/

# Copy worker code and app structure
COPY cmd/entities-worker/worker.py .
COPY cmd/entities-worker/app ./app

# Create models directory and user
RUN mkdir -p /models && useradd --create-home --shell /bin/bash app && \
    chown -R app /models /app

# Switch to app user
USER app

# Expose metrics port
EXPOSE 8002

# Environment variables
ENV PYTHONUNBUFFERED=1
ENV GLINER_MODEL_PATH=/models/gliner-small

# Run worker
CMD ["python", "worker.py"]
