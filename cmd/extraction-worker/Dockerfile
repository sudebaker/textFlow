FROM python:3.11-slim

WORKDIR /app

COPY cmd/extraction-worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pkg/ ./pkg/
COPY cmd/extraction-worker/ ./cmd/extraction-worker/

CMD ["python", "cmd/extraction-worker/worker.py"]
