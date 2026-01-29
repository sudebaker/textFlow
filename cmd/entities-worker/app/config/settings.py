"""
Configuration settings for the entities worker.
"""

from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    """Worker settings with environment variable support."""

    # Redis Configuration
    redis_url: str = Field(
        default="redis://localhost:6379", description="Redis connection URL"
    )

    # Queue Configuration
    entities_queue: str = Field(
        default="entities", description="RabbitMQ queue name for entities extraction"
    )

    class Config:
        env_prefix = "ENTITIES_"
        env_file = ".env"
        case_sensitive = False
