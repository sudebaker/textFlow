FROM python:3.11.12-slim-bookworm

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

COPY cmd/completion-worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create non-root user matching orchestrator UID/GID (100/101) for shared volume access
RUN addgroup --gid 101 appgroup && \
    adduser --disabled-password --gecos "" --uid 100 --ingroup appgroup appuser && \
    chown -R appuser:appgroup /app

COPY pkg/ ./pkg/
COPY cmd/completion-worker/ ./cmd/completion-worker/

USER appuser

CMD ["python", "cmd/completion-worker/worker.py"]
