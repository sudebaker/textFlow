# Multi-stage build for Embeddings Worker - Air-gapped ready
FROM python:3.11-slim as base

# ⭐ AIR-GAPPED MODE: Disable HuggingFace Hub online checks
ENV HF_HUB_OFFLINE=1
ENV TRANSFORMERS_OFFLINE=1
ENV HF_HOME=/models

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    curl \
    && rm -rf /var/lib/apt/lists/*

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

# Create models directory (will be mounted or populated at runtime)
RUN mkdir -p /models && chown -R app:app /app /models

# Switch to app user
USER app

# Environment variables
ENV PYTHONUNBUFFERED=1
ENV MODEL_PATH=/models/bge-m3

EXPOSE 8001

# Run worker
CMD ["python", "worker.py"]