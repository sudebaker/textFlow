"""
Configuration settings for the embeddings service using Pydantic.

This module provides configuration management with environment variable support
and validation for the embeddings service.
"""

import os
from functools import lru_cache
from typing import Optional
from pydantic import Field, validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings with environment variable support."""

    # Model Configuration
    model_name: str = Field(
        default="BAAI/bge-m3",
        description="HuggingFace model name for embeddings"
    )
    model_path: str = Field(
        default=".models/embeddings/bge-m3_model",
        description="Local path to the embedding model"
    )
    embedding_dimension: int = Field(
        default=1024,
        description="Dimension of the embedding vectors"
    )

    # Chunking Configuration
    chunk_size: int = Field(
        default=512,
        gt=0,
        description="Default chunk size for text splitting"
    )
    chunk_overlap: int = Field(
        default=64,
        ge=0,
        description="Default overlap between chunks"
    )
    max_text_size: int = Field(
        default=1048576,  # 1MB
        gt=0,
        description="Maximum allowed text size in bytes"
    )

    # Processing Configuration
    batch_size: int = Field(
        default=32,
        gt=0,
        description="Batch size for embedding generation"
    )
    timeout: int = Field(
        default=30,
        gt=0,
        description="Timeout in seconds for embedding generation"
    )

    # API Configuration
    api_host: str = Field(
        default="0.0.0.0",
        description="Host to bind the API server"
    )
    api_port: int = Field(
        default=8000,
        gt=0,
        le=65535,
        description="Port for the API server"
    )
    api_prefix: str = Field(
        default="",
        description="URL prefix for API endpoints"
    )

    # Logging Configuration
    log_level: str = Field(
        default="INFO",
        description="Logging level (DEBUG, INFO, WARNING, ERROR)"
    )

    # CORS Configuration
    cors_origins: str = Field(
        default="http://localhost:8080",
        description="Comma-separated list of allowed CORS origins"
    )

    @property
    def cors_origins_list(self) -> list:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    # GPU Configuration (optional)
    torch_cuda_arch_list: Optional[str] = Field(
        default=None,
        description="CUDA architecture list for GPU support"
    )
    cuda_visible_devices: Optional[str] = Field(
        default=None,
        description="CUDA visible devices for GPU support"
    )

    class Config:
        env_prefix = "EMBEDDINGS_"
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False

    @validator('log_level')
    def validate_log_level(cls, v):
        """Validate that log_level is one of the allowed values."""
        allowed_levels = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']
        if v.upper() not in allowed_levels:
            raise ValueError(f'log_level must be one of {allowed_levels}')
        return v.upper()

    @validator('chunk_overlap')
    def validate_chunk_overlap(cls, v, values):
        """Validate that chunk_overlap is less than chunk_size."""
        if 'chunk_size' in values and v >= values['chunk_size']:
            raise ValueError('chunk_overlap must be less than chunk_size')
        return v

    def get_model_device(self) -> str:
        """Determine the appropriate device for the model."""
        if self.cuda_visible_devices is not None:
            return "cuda"
        return "cpu"

    def is_gpu_enabled(self) -> bool:
        """Check if GPU is enabled."""
        return self.cuda_visible_devices is not None

    def get_collection_name_pattern(self) -> str:
        """Get the regex pattern for collection name validation."""
        return r"^[a-zA-Z0-9_-]+$"


@lru_cache()
def get_settings() -> Settings:
    """
    Get cached settings instance.

    Returns:
        Settings: Application settings
    """
    return Settings()
