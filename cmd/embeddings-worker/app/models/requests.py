"""
Pydantic models for embedding service requests.

This module contains request models with validation for the embeddings API.
"""

import re
from typing import Dict, Any, Optional
from pydantic import BaseModel, Field, validator


class EmbeddingRequest(BaseModel):
    """
    Request model for generating embeddings from text.
    
    This model defines the input structure for embedding generation
    with chunking support and metadata handling.
    """
    
    text: str = Field(
        ...,
        max_length=1048576,  # 1MB limit
        description="Text content to generate embeddings for"
    )
    collection_name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Name of the collection for Qdrant storage"
    )
    doc_id: Optional[str] = Field(
        None,
        max_length=255,
        description="Optional document identifier"
    )
    chunk_size: int = Field(
        default=512,
        gt=0,
        le=8192,
        description="Size of text chunks for processing"
    )
    chunk_overlap: int = Field(
        default=64,
        ge=0,
        description="Overlap between text chunks"
    )
    metadata: Optional[Dict[str, Any]] = Field(
        None,
        description="Additional metadata to include with embeddings"
    )
    
    @validator('collection_name')
    def validate_collection_name(cls, v):
        """Validate that collection_name contains only allowed characters."""
        if not re.match(r'^[a-zA-Z0-9_-]+$', v):
            raise ValueError(
                'collection_name can only contain alphanumeric characters, '
                'underscores, and hyphens'
            )
        return v
    
    @validator('chunk_overlap')
    def validate_chunk_overlap(cls, v, values):
        """Validate that chunk_overlap is less than chunk_size."""
        if 'chunk_size' in values and v >= values['chunk_size']:
            raise ValueError('chunk_overlap must be less than chunk_size')
        return v
    
    @validator('text')
    def validate_text_not_empty(cls, v):
        """Validate that text is not empty or just whitespace."""
        if not v.strip():
            raise ValueError('text cannot be empty or whitespace only')
        return v
    
    class Config:
        """Pydantic configuration."""
        schema_extra = {
            "example": {
                "text": "This is a sample document text that will be chunked and processed.",
                "collection_name": "my_documents",
                "doc_id": "doc_123",
                "chunk_size": 512,
                "chunk_overlap": 64,
                "metadata": {
                    "source": "pdf",
                    "author": "John Doe",
                    "date": "2023-01-01"
                }
            }
        }