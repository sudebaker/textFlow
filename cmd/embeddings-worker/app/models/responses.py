"""
Pydantic models for embedding service responses.

This module contains response models for the embeddings API
including health checks and error responses.
"""

from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field


class TextChunk(BaseModel):
    """
    Represents a chunk of text with its embedding and metadata.
    
    This model contains the actual text chunk, its embedding vector,
    and associated metadata for storage in Qdrant.
    """
    
    text_chunk: str = Field(
        ...,
        description="The text content of the chunk"
    )
    embedding: List[float] = Field(
        ...,
        description="Embedding vector for the text chunk"
    )
    metadata: Dict[str, Any] = Field(
        ...,
        description="Metadata including chunk_index and inherited metadata"
    )
    
    class Config:
        """Pydantic configuration."""
        schema_extra = {
            "example": {
                "text_chunk": "This is a sample text chunk.",
                "embedding": [0.1, -0.2, 0.3, "..."],
                "metadata": {
                    "chunk_index": 0,
                    "chunk_size": 512,
                    "doc_id": "doc_123",
                    "source": "pdf"
                }
            }
        }


class EmbeddingResponse(BaseModel):
    """
    Response model for embedding generation requests.
    
    This model contains the complete response with chunks,
    processing time, and metadata.
    """
    
    collection_name: str = Field(
        ...,
        description="Name of the collection for Qdrant storage"
    )
    doc_id: Optional[str] = Field(
        None,
        description="Document identifier if provided"
    )
    chunks: List[TextChunk] = Field(
        ...,
        description="List of text chunks with embeddings"
    )
    processing_time_ms: int = Field(
        ...,
        description="Total processing time in milliseconds"
    )
    embedding_dimension: int = Field(
        ...,
        description="Dimension of the embedding vectors"
    )
    total_chunks: int = Field(
        ...,
        description="Total number of chunks generated"
    )
    success: bool = Field(
        default=True,
        description="Whether the processing was successful"
    )
    error: Optional[str] = Field(
        None,
        description="Error message if processing failed"
    )
    
    class Config:
        """Pydantic configuration."""
        schema_extra = {
            "example": {
                "collection_name": "my_documents",
                "doc_id": "doc_123",
                "chunks": [
                    {
                        "text_chunk": "First chunk of text...",
                        "embedding": [0.1, -0.2, 0.3, "..."],
                        "metadata": {
                            "chunk_index": 0,
                            "chunk_size": 512,
                            "doc_id": "doc_123"
                        }
                    }
                ],
                "processing_time_ms": 1500,
                "embedding_dimension": 1024,
                "total_chunks": 1,
                "success": True
            }
        }


class HealthCheck(BaseModel):
    """
    Health check response for the service.
    
    This model contains service health information including
    model status and system metrics.
    """
    
    status: str = Field(
        ...,
        description="Overall health status (healthy, unhealthy)"
    )
    version: str = Field(
        ...,
        description="Service version"
    )
    model_loaded: bool = Field(
        ...,
        description="Whether the embedding model is loaded"
    )
    model_name: str = Field(
        ...,
        description="Name of the loaded model"
    )
    embedding_dimension: int = Field(
        ...,
        description="Dimension of embedding vectors"
    )
    device: str = Field(
        ...,
        description="Device being used (cpu/cuda)"
    )
    checks: Dict[str, str] = Field(
        default_factory=dict,
        description="Detailed health check results"
    )
    
    class Config:
        """Pydantic configuration."""
        schema_extra = {
            "example": {
                "status": "healthy",
                "version": "1.0.0",
                "model_loaded": True,
                "model_name": "BAAI/bge-m3",
                "embedding_dimension": 1024,
                "device": "cpu",
                "checks": {
                    "model": "ok",
                    "memory": "ok",
                    "disk_space": "ok"
                }
            }
        }


class ErrorResponse(BaseModel):
    """
    Standard error response model.
    
    This model provides consistent error responses
    across all API endpoints.
    """
    
    error: str = Field(
        ...,
        description="Error message"
    )
    detail: Optional[str] = Field(
        None,
        description="Detailed error information"
    )
    error_code: Optional[str] = Field(
        None,
        description="Machine-readable error code"
    )
    
    class Config:
        """Pydantic configuration."""
        schema_extra = {
            "example": {
                "error": "Validation error",
                "detail": "chunk_overlap must be less than chunk_size",
                "error_code": "VALIDATION_ERROR"
            }
        }