FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY cmd/image-worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pkg/worker_common/ /app/pkg/worker_common/
COPY pkg/image_client/ /app/pkg/image_client/
COPY pkg/shared/ /app/pkg/shared/
COPY pkg/events_python.py /app/pkg/events_python.py
COPY pkg/logging_python.py /app/pkg/logging_python.py

COPY cmd/image-worker/ /app/

CMD ["python", "worker.py"]