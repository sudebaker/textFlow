FROM python:3.11.12-slim-bookworm

LABEL maintainer="textflow"
LABEL description="Extraction Worker for Unstructured API"

WORKDIR /app

# ⭐ Air-gapped mode
ENV HF_HUB_OFFLINE=1
ENV TRANSFORMERS_OFFLINE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Install system dependencies (exiftool for PDF metadata)
RUN apt-get update && apt-get install -y --no-install-recommends \
    exiftool \
    libmagic1 \
    && rm -rf /var/lib/apt/lists/*

COPY cmd/extraction-worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create non-root user
RUN adduser --disabled-password --gecos "" app && \
    chown -R app:app /app

COPY pkg/ ./pkg/
COPY cmd/extraction-worker/ ./cmd/extraction-worker/
COPY configs/pipeline.json /app/configs/pipeline.json

USER app

CMD ["python", "cmd/extraction-worker/worker.py"]
