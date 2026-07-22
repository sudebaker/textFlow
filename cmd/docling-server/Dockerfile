FROM python:3.11.12-slim-bookworm

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Docling and dependencies
RUN pip install --no-cache-dir \
    docling \
    docling-core \
    pillow \
    pydantic \
    fastapi \
    uvicorn[standard] \
    python-multipart \
    PyPDF2

# Create a simple FastAPI server for Docling
RUN mkdir -p /app

# Create non-root user
RUN adduser --disabled-password --gecos "" app && \
    chown -R app:app /app

COPY cmd/docling-server/docling_server.py /app/docling_server.py

USER app

# Health check
HEALTHCHECK --interval=10s --timeout=5s --retries=10 --start-period=30s \
    CMD curl -f http://localhost:5001/openapi.json || exit 1

EXPOSE 5001

CMD ["uvicorn", "docling_server:app", "--host", "0.0.0.0", "--port", "5001"]
