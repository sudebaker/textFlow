FROM python:3.11.12-slim-bookworm

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

COPY cmd/completion-worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create non-root user
RUN adduser --disabled-password --gecos "" app && \
    chown -R app:app /app

COPY pkg/ ./pkg/
COPY cmd/completion-worker/ ./cmd/completion-worker/

USER app

CMD ["python", "cmd/completion-worker/worker.py"]
