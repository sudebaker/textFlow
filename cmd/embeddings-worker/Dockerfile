# Multi-stage build for Embeddings Worker
FROM python:3.11-slim as base

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    curl \
    wget \
    git \
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

# Change ownership to app user
RUN chown -R app:app /app

# Switch to app user
USER app

# Environment variables
ENV PYTHONUNBUFFERED=1

EXPOSE 8001

# Run worker
CMD ["python", "worker.py"]