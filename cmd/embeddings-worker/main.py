"""
Embeddings Service - FastAPI Application

This is the main entry point for the embeddings generation service.
The service provides text chunking and embedding generation using BAAI/bge-m3 model.
"""

import logging
import sys
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config.settings import get_settings
from app.api.routes.embeddings import router as embeddings_router

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger(__name__)

# Global variables
settings = get_settings()
embedding_service = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle."""
    # Startup
    logger.info("Starting Embeddings Service...")
    logger.info(f"Configuration: {settings.dict()}")
    
    try:
        # Initialize embedding service
        from app.services.embeddings import EmbeddingService
        global embedding_service
        embedding_service = EmbeddingService(
            model_name=settings.model_name,
            model_path=settings.model_path,
            device=settings.get_model_device()
        )
        
        # Preload model if possible
        if embedding_service.load_model():
            logger.info("Embedding model loaded successfully")
        else:
            logger.warning("Failed to preload embedding model")
        
        logger.info("Embeddings Service started successfully")
        yield
        
    except Exception as e:
        logger.error(f"Failed to start embeddings service: {e}")
        sys.exit(1)
    
    # Shutdown
    logger.info("Shutting down Embeddings Service...")
    if embedding_service:
        embedding_service.cleanup()
    logger.info("Embeddings Service stopped")


# Create FastAPI application
app = FastAPI(
    title="Embeddings Service",
    description="""
## Text Chunking and Embedding Generation Service

This service provides high-quality text embeddings using the **BAAI/bge-m3** multilingual model
with support for configurable text chunking. It's designed for integration with RAG (Retrieval-Augmented
Generation) systems and Qdrant vector databases.

### 🌍 Model Features
- **Model**: BAAI/bge-m3 (1024 dimensions)
- **Languages**: 100+ supported languages
- **Performance**: ~50 embeddings/second (CPU), ~500 embeddings/second (GPU)
- **Quality**: Optimized for semantic search and retrieval

### 🔧 Key Features
- **Configurable Chunking**: Customizable chunk size and overlap
- **Batch Processing**: Efficient handling of multiple texts
- **GPU Acceleration**: Optional CUDA support for high throughput
- **Production Ready**: Health checks, monitoring, and Docker support
- **Thread-Safe**: Concurrent request handling
- **Qdrant Ready**: Output format optimized for vector storage

### 📊 API Endpoints

| Endpoint | Method | Description |
|----------|---------|-------------|
| `/api/v1/embed` | POST | Generate embeddings with chunking |
| `/api/v1/health` | GET | Service health status |
| `/api/v1/info` | GET | Service information and model details |
| `/api/v1/stats` | GET | Chunking statistics (no generation) |

### 🎯 Use Cases
- **Document Processing**: Split and embed large documents
- **RAG Systems**: Prepare text chunks for retrieval
- **Semantic Search**: Generate embeddings for similarity matching
- **Multilingual Content**: Process text in multiple languages

### ⚡ Performance Tips
- Use **batch processing** for multiple texts
- Optimal **chunk size**: 256-512 characters
- Enable **GPU acceleration** for high-throughput scenarios
- Monitor **memory usage** for large document processing

---
*Built with ❤️ for the journalist-agent ecosystem*
""",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_components={
        "securitySchemes": {
            "bearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT"
            }
        }
    },
    contact={
        "name": "Journalist Agent Team",
        "url": "https://github.com/amphora/journalist-agent",
        "email": "team@journalist-agent.com"
    },
    license_info={
        "name": "MIT License",
        "url": "https://opensource.org/licenses/MIT"
    },
    terms_of_service="https://github.com/amphora/journalist-agent/blob/main/LICENSE"
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Authorization"],
)


# Request logging middleware
@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log incoming requests."""
    start_time = time.time()
    
    # Log request
    logger.info(
        f"Request: {request.method} {request.url.path} "
        f"from {request.client.host if request.client else 'unknown'}"
    )
    
    # Process request
    response = await call_next(request)
    
    # Log response
    process_time = time.time() - start_time
    logger.info(
        f"Response: {response.status_code} "
        f"in {process_time:.3f}s"
    )
    
    return response


# Include routers
app.include_router(embeddings_router)


# Root endpoint
@app.get("/")
async def root():
    """Root endpoint with service information."""
    return {
        "service": "Embeddings Service",
        "version": "1.0.0",
        "status": "running",
        "endpoints": {
            "embed": "/api/v1/embed",
            "health": "/api/v1/health",
            "info": "/api/v1/info",
            "docs": "/docs"
        }
    }


# Health check endpoint (simplified version)
@app.get("/health")
async def simple_health():
    """Simple health check endpoint."""
    return {
        "status": "healthy",
        "service": "embeddings-service",
        "version": "1.0.0"
    }


# Exception handlers
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Handle unhandled exceptions."""
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal server error",
            "detail": str(exc)
        }
    )


# Import time for request logging
import time


def main():
    """Main function to run the application."""
    import uvicorn
    
    logger.info(f"Starting Embeddings Service on {settings.api_host}:{settings.api_port}")
    logger.info(f"Environment: {os.environ.get('ENV', 'development')}")
    logger.info(f"Model: {settings.model_name}")
    logger.info(f"Device: {settings.get_model_device()}")
    
    # Run uvicorn server
    uvicorn.run(
        "main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=os.environ.get("ENV") == "development",
        log_level=settings.log_level.lower(),
        access_log=True
    )


if __name__ == "__main__":
    main()