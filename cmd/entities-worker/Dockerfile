# GLiNER Entity Extraction Worker - Python
# Multi-stage build for air-gapped deployment

# Final image (no unused builder stage)
FROM python:3.11.12-slim-bookworm

LABEL maintainer="textflow"
LABEL description="GLiNER Entity Extraction Worker for NER tasks"

# Environment setup
ENV HF_HOME=/home/app/.cache/huggingface
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create app user and directories early
RUN useradd --create-home --shell /bin/bash app \
    && mkdir -p /models /home/app/.cache/huggingface

# Copy requirements
COPY cmd/entities-worker/requirements-base.txt ./requirements.txt

# Install Python dependencies (excluding torch)
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
    && pip install --no-cache-dir -r requirements.txt

# Install PyTorch: CUDA optional via build arg, defaults to CPU
# Usage: docker build --build-arg CUDA_VERSION=cu118 (for GPU)
#        docker build                                 (for CPU)
ARG CUDA_VERSION=""
RUN if [ -n "$CUDA_VERSION" ]; then \
        pip install --no-cache-dir torch \
            --index-url https://download.pytorch.org/whl/${CUDA_VERSION}; \
    else \
        pip install --no-cache-dir torch; \
    fi

# Enforce offline mode - models are pre-downloaded and mounted as volumes
ENV HF_HUB_OFFLINE=1
ENV TRANSFORMERS_OFFLINE=1

# Copy shared packages (events_python)
COPY pkg/ ./pkg/

# Copy worker code, sliding window, and app structure
COPY cmd/entities-worker/entities_worker.py .
COPY cmd/entities-worker/sliding_window.py .
COPY cmd/entities-worker/entrypoint.sh /entrypoint.sh
COPY cmd/entities-worker/app ./app

# Fix permissions
RUN mkdir -p /app/data/artifacts && \
    chown -R app:app /models /app /home/app && chmod +x /entrypoint.sh

# Switch to app user
USER app

# Expose metrics port
EXPOSE 8002

# Run entrypoint script
CMD ["/entrypoint.sh"]
