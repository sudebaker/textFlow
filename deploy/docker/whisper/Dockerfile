FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1 \
    DEVICE=cpu \
    COMPUTE_TYPE=int8 \
    MODEL_SIZE=large-v2 \
    PORT=8080 \
    MODEL_PATH=/models

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && rm -rf ~/.cache/pip/*

COPY app ./app

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:${PORT}/health || exit 1

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]