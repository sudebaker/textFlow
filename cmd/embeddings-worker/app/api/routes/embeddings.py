"""
API routes for embeddings service.

This module provides FastAPI routes for embedding generation,
health checks, and service information.
"""

import time
import logging
from typing import Dict, Any
from fastapi import APIRouter, HTTPException, status, Query
from fastapi.responses import JSONResponse

from app.models.requests import EmbeddingRequest
from app.models.responses import EmbeddingResponse, TextChunk, HealthCheck, ErrorResponse
from app.services.chunking import ChunkingService
from app.services.embeddings import EmbeddingService

logger = logging.getLogger(__name__)

# Create router
router = APIRouter(prefix="/api/v1", tags=["embeddings"])

# Initialize services
chunking_service = ChunkingService()
embedding_service = None


def get_embedding_service(settings) -> EmbeddingService:
    """Get or create embedding service instance."""
    global embedding_service
    if embedding_service is None:
        embedding_service = EmbeddingService(
            model_name=settings.model_name,
            model_path=settings.model_path,
            device=settings.get_model_device()
        )
    return embedding_service


@router.post(
    "/embed",
    response_model=EmbeddingResponse,
    summary="Generate embeddings for text",
    description="Generate high-quality embeddings for text using BAAI/bge-m3 multilingual model. This endpoint automatically chunks the input text and generates embeddings for each chunk. The output is optimized for storage in Qdrant vector databases and RAG systems.",
    response_description="Embedding generation results with chunks and vectors"
)
async def generate_embeddings(
    request: EmbeddingRequest,
    settings
) -> EmbeddingResponse:
    """
    Generate embeddings for text with chunking.
    
    This endpoint receives text and metadata, chunks text,
    generates embeddings for each chunk, and returns results
    ready for storage in Qdrant.
    
    Args:
        request: Embedding request with text and parameters
        settings: Application settings
        
    Returns:
        EmbeddingResponse with chunks and embeddings
        
    Raises:
        HTTPException: If processing fails
    """
    start_time = time.time()
    
    try:
        # Validate request parameters
        chunking_service.validate_chunking_parameters(
            text=request.text,
            chunk_size=request.chunk_size,
            chunk_overlap=request.chunk_overlap
        )
        
        logger.info(
            f"Processing embedding request: "
            f"collection={request.collection_name}, "
            f"text_length={len(request.text)}, "
            f"chunk_size={request.chunk_size}, "
            f"overlap={request.chunk_overlap}"
        )
        
        # Get embedding service
        embed_service = get_embedding_service(settings)
        
        # Chunk text
        chunks_data = chunking_service.chunk_text(
            text=request.text,
            chunk_size=request.chunk_size,
            chunk_overlap=request.chunk_overlap,
            doc_id=request.doc_id,
            metadata=request.metadata
        )
        
        if not chunks_data:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No chunks were generated from text"
            )
        
        # Extract text from chunks for embedding generation
        chunk_texts = [chunk["text"] for chunk in chunks_data]
        
        logger.debug(f"Generated {len(chunk_texts)} chunks for embedding")
        
        # Generate embeddings for all chunks
        embeddings = embed_service.generate_embeddings(
            texts=chunk_texts,
            batch_size=settings.batch_size
        )
        
        # Verify we got embeddings for all chunks
        if len(embeddings) != len(chunks_data):
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Embedding count mismatch: {len(embeddings)} vs {len(chunks_data)}"
            )
        
        # Create TextChunk objects
        text_chunks = []
        for i, (chunk_data, embedding) in enumerate(zip(chunks_data, embeddings)):
            text_chunk = TextChunk(
                text_chunk=chunk_data["text"],
                embedding=embedding,
                metadata=chunk_data["metadata"]
            )
            text_chunks.append(text_chunk)
        
        processing_time = int((time.time() - start_time) * 1000)
        
        response = EmbeddingResponse(
            collection_name=request.collection_name,
            doc_id=request.doc_id,
            chunks=text_chunks,
            processing_time_ms=processing_time,
            embedding_dimension=settings.embedding_dimension,
            total_chunks=len(text_chunks),
            success=True
        )
        
        logger.info(
            f"Embedding generation completed: "
            f"{len(text_chunks)} chunks, "
            f"{processing_time}ms, "
            f"collection={request.collection_name}"
        )
        
        return response
        
    except ValueError as e:
        logger.warning(f"Validation error: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        logger.error(f"Embedding generation failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Embedding generation failed: {str(e)}"
        )


@router.get(
    "/health",
    response_model=HealthCheck,
    summary="Check service health",
    description="Check the health status of the embeddings service including model loading status and system checks."
)
async def health_check(settings) -> HealthCheck:
    """
    Check health of embeddings service.
    
    Returns:
        HealthCheck with service status and detailed checks
    """
    try:
        # Get embedding service
        embed_service = get_embedding_service(settings)
        
        # Perform health checks
        checks = embed_service.health_check()
        
        # Get model info
        model_info = embed_service.get_model_info()
        
        # Determine overall status
        is_healthy = all(
            check_status == "ok" 
            for check_status in checks.values()
            if not check_status.startswith("unavailable")
        )
        
        overall_status = "healthy" if is_healthy else "unhealthy"
        
        return HealthCheck(
            status=overall_status,
            version="1.0.0",
            model_loaded=model_info["model_loaded"],
            model_name=model_info["model_name"],
            embedding_dimension=model_info["embedding_dimension"],
            device=model_info["device"],
            checks=checks
        )
        
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return HealthCheck(
            status="unhealthy",
            version="1.0.0",
            model_loaded=False,
            model_name=settings.model_name,
            embedding_dimension=settings.embedding_dimension,
            device=settings.get_model_device(),
            checks={"health_check": f"failed: {e}"}
        )


@router.get(
    "/info",
    summary="Get service information",
    description="Get detailed information about the embeddings service including model details and configuration."
)
async def service_info(settings) -> Dict[str, Any]:
    """
    Get information about embeddings service.
    
    Returns:
        Dictionary with service information
    """
    try:
        embed_service = get_embedding_service(settings)
        model_info = embed_service.get_model_info()
        memory_info = embed_service.get_memory_usage()
        
        return {
            "service": "embeddings-service",
            "version": "1.0.0",
            "model": model_info,
            "memory": memory_info,
            "settings": {
                "chunk_size": settings.chunk_size,
                "chunk_overlap": settings.chunk_overlap,
                "max_text_size": settings.max_text_size,
                "batch_size": settings.batch_size,
                "timeout": settings.timeout
            }
        }
        
    except Exception as e:
        logger.error(f"Failed to get service info: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get service info: {str(e)}"
        )


@router.get(
    "/stats",
    summary="Get chunking statistics",
    description="Get chunking statistics for text without generating embeddings. Useful for planning and optimization purposes."
)
async def chunking_stats(
    text: str = Query(..., description="Text to analyze"),
    chunk_size: int = Query(default=512, description="Desired chunk size"),
    chunk_overlap: int = Query(default=64, description="Desired overlap")
) -> Dict[str, Any]:
    """
    Get chunking statistics without generating embeddings.
    
    Useful for planning and optimization purposes.
    
    Args:
        text: Text to analyze
        chunk_size: Desired chunk size
        chunk_overlap: Desired overlap
        
    Returns:
        Dictionary with chunking statistics
    """
    try:
        stats = chunking_service.get_chunk_statistics(
            text=text,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap
        )
        
        return {
            "input_parameters": {
                "text_length": len(text),
                "chunk_size": chunk_size,
                "chunk_overlap": chunk_overlap
            },
            "statistics": stats
        }
        
    except Exception as e:
        logger.error(f"Failed to get chunking stats: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )


# Exception handlers
@router.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    """Handle HTTP exceptions with consistent error response."""
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorResponse(
            error=exc.detail,
            error_code=f"HTTP_{exc.status_code}"
        ).dict()
    )


@router.exception_handler(Exception)
async def general_exception_handler(request, exc):
    """Handle general exceptions with consistent error response."""
    logger.error(f"Unhandled exception: {exc}")
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=ErrorResponse(
            error="Internal server error",
            detail=str(exc),
            error_code="INTERNAL_ERROR"
        ).dict()
    )