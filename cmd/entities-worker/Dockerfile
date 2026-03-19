# GLiNER Entity Extraction Worker - Python
# Multi-stage build for air-gapped deployment

# Stage 1: Builder (for any compilation needs)
FROM python:3.11.12-slim-bookworm as builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# Stage 2: Final image
FROM python:3.11.12-slim-bookworm

LABEL maintainer="ia-text-orchestrator"
LABEL description="GLiNER Entity Extraction Worker for NER tasks"

# Environment setup
ENV HF_HOME=/home/app/.cache/huggingface
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

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

# Install PyTorch: CUDA optional via build arg, defaults to CPU
# Usage: docker build --build-arg CUDA_VERSION=cu118 (for GPU)
#        docker build                                 (for CPU)
ARG CUDA_VERSION=""
RUN if [ -n "$CUDA_VERSION" ]; then \
        pip install --no-cache-dir torch \
            --index-url https://download.pytorch.org/whl/${CUDA_VERSION}; \
    fi

# Enforce offline mode - models are pre-downloaded and mounted as volumes
ENV HF_HUB_OFFLINE=1
ENV TRANSFORMERS_OFFLINE=1

# Copy shared packages (events_python)
COPY pkg/ ./pkg/

# Copy worker code, sliding window, and app structure
COPY cmd/entities-worker/worker.py .
COPY cmd/entities-worker/sliding_window.py .
COPY cmd/entities-worker/entrypoint.sh /entrypoint.sh
COPY cmd/entities-worker/app ./app

# Fix permissions
RUN chown -R app:app /models /app /home/app && chmod +x /entrypoint.sh

# Switch to app user
USER app

# Expose metrics port
EXPOSE 8002

# Run entrypoint script
CMD ["/entrypoint.sh"]
