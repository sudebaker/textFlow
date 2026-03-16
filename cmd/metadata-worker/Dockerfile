FROM python:3.11.12-slim-bookworm

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Copy requirements and install dependencies
COPY cmd/metadata-worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy shared packages (events_python)
COPY pkg/ ./pkg/

# Copy worker code
COPY cmd/metadata-worker/worker.py .

# Create non-root user
RUN useradd -m -u 1000 worker && chown -R worker:worker /app
USER worker

# Expose Prometheus metrics port
EXPOSE 8003

# Run worker
CMD ["python", "worker.py"]
