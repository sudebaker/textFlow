# Embeddings Service

Text chunking and embedding generation service using BAAI/bge-m3 multilingual model.

## Overview

This service provides:
- **Text chunking** with configurable size and overlap
- **Embedding generation** using BAAI/bge-m3 multilingual model (1024 dimensions)
- **REST API** with FastAPI
- **CPU/GPU support** for flexibility
- **Production-ready** with health checks and monitoring

## Features

- 🌍 **Multilingual Support**: BAAI/bge-m3 supports 100+ languages
- 📊 **1024 Dimensions**: High-quality embedding vectors
- 🔧 **Configurable Chunking**: Customizable chunk size and overlap
- 🚀 **High Performance**: Batch processing and GPU acceleration
- 📝 **Structured Logging**: Request/response logging with metrics
- 🔍 **Health Monitoring**: Model health and system metrics
- 🐳 **Docker Ready**: Multi-stage build with model preloading

## API Endpoints

### POST /api/v1/embed
Generate embeddings for text with chunking.

**Request:**
```json
{
  "text": "Your text content here...",
  "collection_name": "my_documents",
  "doc_id": "doc_123",
  "chunk_size": 512,
  "chunk_overlap": 64,
  "metadata": {
    "source": "pdf",
    "author": "John Doe"
  }
}
```

**Response:**
```json
{
  "collection_name": "my_documents",
  "doc_id": "doc_123",
  "chunks": [
    {
      "text_chunk": "First chunk of text...",
      "embedding": [0.1, -0.2, 0.3, ...],
      "metadata": {
        "chunk_index": 0,
        "chunk_size": 512,
        "overlap": 0,
        "doc_id": "doc_123",
        "source": "pdf"
      }
    }
  ],
  "processing_time_ms": 1500,
  "embedding_dimension": 1024,
  "total_chunks": 1,
  "success": true
}
```

### GET /api/v1/health
Health check endpoint with detailed service status.

### GET /api/v1/info
Service information including model details and configuration.

### GET /api/v1/stats
Chunking statistics without generating embeddings.

## Configuration

Environment variables:

```bash
# Model Configuration
EMBEDDINGS_MODEL_NAME=BAAI/bge-m3
EMBEDDINGS_MODEL_PATH=.models/embeddings/bge-m3_model
EMBEDDINGS_DIMENSION=1024

# Chunking Configuration
EMBEDDINGS_CHUNK_SIZE=512
EMBEDDINGS_CHUNK_OVERLAP=64
EMBEDDINGS_MAX_TEXT_SIZE=1048576

# Processing Configuration
EMBEDDINGS_BATCH_SIZE=32
EMBEDDINGS_TIMEOUT=30

# API Configuration
EMBEDDINGS_API_HOST=0.0.0.0
EMBEDDINGS_API_PORT=8000
EMBEDDINGS_LOG_LEVEL=INFO

# GPU Configuration (optional)
EMBEDDINGS_TORCH_CUDA_ARCH_LIST=6.0;6.1;7.0;7.5;8.0;8.6+PTX
EMBEDDINGS_CUDA_VISIBLE_DEVICES=0
```

## Installation

### Local Development

```bash
# Clone repository
git clone <repository-url>
cd journalist-agent/cmd/embeddings-service

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Download model
python3 scripts/download_embeddings_model.py --output-dir .models/embeddings

# Run service
python main.py
```

### Docker

```bash
# Build image
docker build -t embeddings-service .

# Run with CPU
docker run -p 8000:8000 \
  -v $(pwd)/.models/embeddings:/models:ro \
  embeddings-service

# Run with GPU (requires nvidia-docker)
docker run --gpus all -p 8000:8000 \
  -v $(pwd)/.models/embeddings:/models:ro \
  embeddings-service
```

### Docker Compose

```bash
# CPU version
docker compose -f docker-compose.yml -f docker-compose.embeddings.yml up embeddings-service

# GPU version
docker compose -f docker-compose.yml -f docker-compose.embeddings.yml --profile gpu up embeddings-service-gpu
```

## Usage Examples

### Using curl

```bash
# Generate embeddings
curl -X POST "http://localhost:8000/api/v1/embed" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "This is a sample document for embedding generation.",
    "collection_name": "test_collection",
    "chunk_size": 256,
    "chunk_overlap": 32
  }'

# Health check
curl "http://localhost:8000/api/v1/health"
```

### Using Python

```python
import requests

# Generate embeddings
response = requests.post(
    "http://localhost:8000/api/v1/embed",
    json={
        "text": "Your document text here...",
        "collection_name": "my_documents",
        "chunk_size": 512
    }
)

result = response.json()
print(f"Generated {len(result['chunks'])} embeddings")
```

## Performance

### Benchmarks

- **CPU (4 cores)**: ~50 embeddings/second
- **GPU (RTX 3080)**: ~500 embeddings/second
- **Memory usage**: ~4GB (CPU), ~6GB (GPU)
- **Model size**: ~2.2GB

### Optimization Tips

1. **Batch Processing**: Send multiple texts in one request when possible
2. **Chunk Size**: Use 256-512 for optimal performance
3. **GPU**: Enable GPU acceleration for high-throughput scenarios
4. **Model Preloading**: Use Docker build-time model download

## Development

### Running Tests

```bash
# Install test dependencies
pip install pytest pytest-asyncio httpx

# Run tests
pytest tests/ -v
```

### Project Structure

```
cmd/embeddings-service/
├── main.py                 # FastAPI application entry point
├── requirements.txt        # Python dependencies
├── Dockerfile             # Multi-stage Docker build
├── README.md              # This file
├── .env.example           # Environment variables template
├── app/
│   ├── config/
│   │   └── settings.py    # Configuration with Pydantic
│   ├── models/
│   │   ├── requests.py    # Pydantic request models
│   │   └── responses.py   # Pydantic response models
│   ├── services/
│   │   ├── embeddings.py  # Embedding generation service
│   │   └── chunking.py    # Text chunking service
│   └── api/
│       └── routes/
│           └── embeddings.py  # API endpoints
├── tests/                 # Test files
└── scripts/
    └── download_embeddings_model.py   # Model download script
```

## Monitoring

The service provides structured logging with the following information:

- Request/response timing
- Processing statistics
- Model health status
- Error details and stack traces
- Memory usage (GPU)

Log levels: DEBUG, INFO, WARNING, ERROR

## Security

- Input validation with Pydantic models
- Text size limits (1MB max)
- Collection name validation
- Error handling without information leakage
- Non-root Docker user

## License

This service is part of the journalist-agent project.