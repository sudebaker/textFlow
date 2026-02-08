FROM python:3.11-slim

LABEL maintainer="ia-text-orchestrator"
LABEL description="Extraction Worker for Unstructured API"

WORKDIR /app

# ⭐ Air-gapped mode
ENV HF_HUB_OFFLINE=1
ENV TRANSFORMERS_OFFLINE=1

# Install system dependencies (exiftool for PDF metadata)
RUN apt-get update && apt-get install -y --no-install-recommends \
    exiftool \
    libmagic1 \
    && rm -rf /var/lib/apt/lists/*

COPY cmd/extraction-worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pkg/ ./pkg/
COPY cmd/extraction-worker/ ./cmd/extraction-worker/

CMD ["python", "cmd/extraction-worker/worker.py"]
