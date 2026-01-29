FROM python:3.11-slim

WORKDIR /app

COPY cmd/completion-worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pkg/ ./pkg/
COPY cmd/completion-worker/ ./cmd/completion-worker/

CMD ["python", "cmd/completion-worker/worker.py"]
