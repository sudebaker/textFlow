# GLiNER Entity Extraction Worker - Python
FROM python:3.11-slim

LABEL maintainer="ia-text-orchestrator"
LABEL description="GLiNER Entity Extraction Worker for NER tasks"

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    curl \
    wget \
    git \
    && rm -rf /var/lib/apt/lists/*

# Create app user
RUN useradd --create-home --shell /bin/bash app

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY cmd/entities-worker/requirements.txt .

# Install PyTorch CPU-only version first (much smaller than GPU version)
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install torch --index-url https://download.pytorch.org/whl/cpu

# Install remaining Python dependencies
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-cache-dir -r requirements.txt

# Copy shared packages (events_python)
COPY pkg/ ./pkg/

# Copy worker code and app structure
COPY cmd/entities-worker/worker.py .
COPY cmd/entities-worker/app ./app

# Create model directory
RUN mkdir -p /models && chown -R app:app /app /models

# Switch to app user
USER app

# Expose metrics port
EXPOSE 8002

# Set environment variables
ENV GLINER_MODEL_PATH=/models/gliner_model
ENV PYTHONUNBUFFERED=1

# Run worker
CMD ["python", "worker.py"]
