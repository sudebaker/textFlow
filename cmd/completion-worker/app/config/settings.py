"""
Configuration settings for the completion worker.
"""

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Worker settings with environment variable support."""

    model_config = {"env_file": ".env", "case_sensitive": False, "extra": "ignore"}

    # Redis Configuration
    redis_url: str = Field(
        default="redis://localhost:6379", description="Redis connection URL"
    )

    # Queue Configuration
    completion_queue: str = Field(
        default="completion", description="RabbitMQ queue name for completion jobs"
    )

    # Deduplication Configuration
    fuzzy_match_threshold: float = Field(
        default=0.85,
        description="Fuzzy matching threshold 0.0-1.0 for entity deduplication (same label required)",
    )
