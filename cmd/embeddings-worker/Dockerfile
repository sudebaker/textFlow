# Multi-stage build for Embeddings Worker - GPU Enabled
# ⭐ Auto-detects CUDA and installs appropriate PyTorch version
FROM python:3.11.12-slim-bookworm as base

# ⭐ AIR-GAPPED MODE: Disable HuggingFace Hub online checks
ENV HF_HUB_OFFLINE=1
ENV TRANSFORMERS_OFFLINE=1
ENV HF_HOME=/models
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# CUDA CONFIGURATION (set by docker-compose override for GPU deployments)
# ENV CUDA_VISIBLE_DEVICES will be set at runtime, not in image

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install PyTorch: CUDA optional via build arg, defaults to CPU
# Usage: docker build --build-arg CUDA_VERSION=cu118 (for GPU)
#        docker build                                 (for CPU)
ARG CUDA_VERSION=""
RUN if [ -n "$CUDA_VERSION" ]; then \
        pip install --no-cache-dir \
            torch torchvision torchaudio \
            --index-url https://download.pytorch.org/whl/${CUDA_VERSION}; \
    else \
        pip install --no-cache-dir torch torchvision torchaudio; \
    fi

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY cmd/embeddings-worker/requirements-base.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Create app user
RUN useradd --create-home --shell /bin/bash app

# Copy shared packages (events_python)
COPY pkg/ ./pkg/

# Copy worker code and app structure
COPY cmd/embeddings-worker/worker.py .
COPY cmd/embeddings-worker/app ./app

# Create models directory
RUN mkdir -p /models && chown -R app:app /app /models

# Switch to app user
USER app

# Environment variables
ENV MODEL_PATH=/models/bge-m3
ENV EMBEDDING_BATCH_SIZE_GPU=32
ENV EMBEDDING_BATCH_SIZE_CPU=2

EXPOSE 8001

# Run worker
CMD ["python", "worker.py"]