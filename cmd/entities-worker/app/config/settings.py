"""
Configuration settings for the entities worker.
"""

from pydantic_settings import BaseSettings
from pydantic import Field
from typing import Dict


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

    # GLiNER Model Configuration
    gliner_model_path: str = Field(
        default="/models/gliner_large",
        description="Path to GLiNER model (local or HuggingFace model name)",
    )

    gliner_model_size: str = Field(
        default="large", description="Model size: small, base, large"
    )

    # Entity Types Configuration
    entity_types: str = Field(
        default="PER,ORG,LOC,DATE,MONEY",
        description="Comma-separated list of entity types to extract",
    )

    # Thresholds Configuration (per entity type)
    entity_threshold_per: float = Field(
        default=0.35, description="Confidence threshold for PERSON entities"
    )

    entity_threshold_org: float = Field(
        default=0.50, description="Confidence threshold for ORGANIZATION entities"
    )

    entity_threshold_loc: float = Field(
        default=0.50, description="Confidence threshold for LOCATION entities"
    )

    entity_threshold_date: float = Field(
        default=0.60, description="Confidence threshold for DATE entities"
    )

    entity_threshold_money: float = Field(
        default=0.65, description="Confidence threshold for MONEY entities"
    )

    # Deduplication Configuration
    deduplication_enabled: bool = Field(
        default=True, description="Enable deduplication of extracted entities"
    )

    fuzzy_match_threshold: float = Field(
        default=0.85, description="Fuzzy matching threshold for deduplication (0.0-1.0)"
    )

    # Model Loading Configuration
    allow_remote_download: bool = Field(
        default=True,
        description="Allow downloading model from HuggingFace if not found locally",
    )

    def get_threshold_map(self) -> Dict[str, float]:
        """Get mapping of entity types to their thresholds."""
        return {
            "PER": self.entity_threshold_per,
            "ORG": self.entity_threshold_org,
            "LOC": self.entity_threshold_loc,
            "DATE": self.entity_threshold_date,
            "MONEY": self.entity_threshold_money,
        }

    class Config:
        env_prefix = "ENTITIES_"
        env_file = ".env"
        case_sensitive = False
