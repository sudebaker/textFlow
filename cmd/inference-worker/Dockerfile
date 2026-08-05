FROM python:3.11.12-slim-bookworm

LABEL maintainer="textflow"
LABEL description="Inference Worker for Micro-Inferences"

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY cmd/inference-worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN adduser --disabled-password --gecos "" app && \
    chown -R app:app /app

COPY pkg/ ./pkg/
COPY cmd/inference-worker/ ./cmd/inference-worker/

USER app

CMD ["python", "cmd/inference-worker/inference_worker.py"]
