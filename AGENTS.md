# AGENTS.md - Development Guidelines for IA Text Orchestrator

This document provides guidelines for AI agents working on this codebase. The project consists of two FastAPI microservices for NLP processing.

## Project Structure

```
ia-text-ochestrator/
├── embeddings-service/    # Text embedding generation (BAAI/bge-m3)
│   ├── app/
│   │   ├── api/routes/     # FastAPI endpoints
│   │   ├── config/         # Settings with Pydantic
│   │   ├── models/         # Pydantic request/response models
│   │   └── services/       # Business logic
│   ├── tests/              # Test files
│   └── main.py             # Application entry point
├── gliner-service/         # Named entity extraction (GLiNER)
│   ├── tests/              # Test files
│   └── main.py             # Application entry point
```

## Build/Lint/Test Commands

### Embeddings Service
```bash
cd embeddings-service
pip install -r requirements.txt
python main.py                          # Run service on port 8000
pytest tests/ -v                        # Run all tests
pytest tests/test_chunking.py -v        # Run single test file
pytest tests/test_chunking.py::TestChunkingService::test_basic_chunking -v  # Run single test
```

### GLiNER Service
```bash
cd gliner-service
pip install -r requirements.txt
pip install -r dev-requirements.txt     # Includes pytest, httpx
python main.py                          # Run service on port 8080
python download_gliner_models.py --output-dir ./models
pytest cmd/gliner-service/tests -q     # Run all tests
pytest cmd/gliner-service/tests/test_api.py::test_extract_success -q  # Single test
```

### Docker Commands
```bash
# Build embeddings service
cd embeddings-service && docker build -t embeddings-service .

# Build GLiNER service
cd gliner-service && docker build -t gliner-service .
```

## Code Style Guidelines

### Imports
- Organize imports in three sections: stdlib, third-party, local
- Sort alphabetically within each section
- Use absolute imports with `app.` prefix for local modules
```python
import logging
import sys
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from app.config.settings import get_settings
from app.api.routes.embeddings import router as embeddings_router
```

### Formatting
- Use 4 spaces for indentation (no tabs)
- Maximum line length: 120 characters
- Use trailing commas in multi-line calls/literals
- Add blank lines between function definitions and classes

### Types
- Use Python 3.11+ type hints throughout
- Use `List[T]`, `Dict[K, V]`, `Optional[T]` from typing
- Use Pydantic `Field` for model validation with descriptions
```python
from typing import List, Dict, Any, Optional
from pydantic import Field, validator

class Settings(BaseSettings):
    model_name: str = Field(default="BAAI/bge-m3", description="HuggingFace model name")
    embedding_dimension: int = Field(default=1024, gt=0, description="Vector dimension")
```

### Naming Conventions
- **Classes**: PascalCase (e.g., `EmbeddingService`, `ChunkingService`)
- **Functions/Variables**: snake_case (e.g., `generate_embeddings`, `chunk_size`)
- **Constants**: UPPER_SNAKE_CASE (e.g., `DEFAULT_ENTITY_TYPES`)
- **Private members**: Leading underscore (e.g., `_model_loaded`)
- **Module docstrings**: Triple quotes at top of file

### Error Handling
- Use Pydantic validation for request data (raises HTTPException 400)
- Use try/except with specific exception types first
- Log errors with `logger.error()` and include context
- Return consistent error responses with `ErrorResponse` model
- Use `HTTPException(status_code=..., detail=...)` in routes
```python
try:
    # business logic
except ValueError as e:
    logger.warning(f"Validation error: {e}")
    raise HTTPException(status_code=400, detail=str(e))
except Exception as e:
    logger.error(f"Embedding generation failed: {e}")
    raise HTTPException(status_code=500, detail=f"Failed: {str(e)}")
```

### FastAPI Patterns
- Use `asynccontextmanager` for application lifespan
- Create routers with `APIRouter(prefix="/api/v1", tags=["name"])`
- Use `response_model` for type-safe responses
- Include detailed docstrings for route documentation
```python
@router.post(
    "/embed",
    response_model=EmbeddingResponse,
    summary="Generate embeddings for text",
    description="Generate high-quality embeddings using BAAI/bge-m3 model."
)
async def generate_embeddings(request: EmbeddingRequest) -> EmbeddingResponse:
    """Generate embeddings for text with chunking."""
    # implementation
```

### Configuration
- Use `pydantic_settings.BaseSettings` for environment configuration
- Set `env_prefix` for environment variable names (e.g., `EMBEDDINGS_`)
- Use `@validator` for cross-field validation
- Cache settings with `@lru_cache()` for singletons
```python
class Settings(BaseSettings):
    api_host: str = Field(default="0.0.0.0")
    api_port: int = Field(default=8000, gt=0, le=65535)

    class Config:
        env_prefix = "EMBEDDINGS_"
        env_file = ".env"

@lru_cache()
def get_settings() -> Settings:
    return Settings()
```

### Testing
- Use `pytest` for all tests
- Follow `test_<module>.py` naming convention
- Use `TestClass` pattern for grouping related tests
- Use `setup_method` for test fixtures
- Test both success and error cases
- Use `pytest.raises()` for exception testing

### Logging
- Configure logging at module level with consistent format
- Use `logging.getLogger(__name__)` per module
- Log at appropriate levels: DEBUG, INFO, WARNING, ERROR
- Include relevant context in log messages
```python
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
```

### Performance Considerations
- Load ML models once at startup (not per request)
- Use threading.Lock for thread-safe model access
- Preload models in lifespan when possible
- Consider batch processing for multiple inputs
- Use `normalize_embeddings=True` for similarity search

### Docker Best Practices
- Use multi-stage builds for smaller images
- Pre-download models at build time
- Run as non-root user in production
- Set appropriate resource limits