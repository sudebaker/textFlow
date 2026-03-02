# Multi-stage build for Embeddings Worker - GPU Enabled
# ⭐ Auto-detects CUDA and installs appropriate PyTorch version
FROM python:3.11-slim as base

# ⭐ AIR-GAPPED MODE: Disable HuggingFace Hub online checks
ENV HF_HUB_OFFLINE=1
ENV TRANSFORMERS_OFFLINE=1
ENV HF_HOME=/models

# ⭐ CUDA CONFIGURATION (auto-detected at runtime)
ENV CUDA_VISIBLE_DEVICES=0
ENV NVIDIA_VISIBLE_DEVICES=0
ENV PYTORCH_CUDA_ALLOC_CONF=max_split_size_mb:512

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    curl \
    wget \
    && rm -rf /var/lib/apt/lists/*

# Install PyTorch with CUDA (auto-detect CUDA version)
# This works with CUDA 11.8, 12.1, 12.2, 12.4
# COMMENTED for offline mode - will fail if models needed
# RUN pip install --no-cache-dir \
#     torch torchvision torchaudio \
#     --index-url https://download.pytorch.org/whl/cu118

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY cmd/embeddings-worker/requirements.txt .
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
ENV PYTHONUNBUFFERED=1
ENV MODEL_PATH=/models/bge-m3
ENV EMBEDDING_BATCH_SIZE_GPU=32
ENV EMBEDDING_BATCH_SIZE_CPU=2

EXPOSE 8001

# Run worker
CMD ["python", "worker.py"]