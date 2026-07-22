"""
Configuration utilities for workers.
"""

import os
from typing import Optional


def get_env(key: str, default: Optional[str] = None, required: bool = False) -> str:
    """
    Get environment variable with validation.

    Args:
        key: Environment variable name
        default: Default value if not set
        required: If True, raises ValueError if not set

    Returns:
        Environment variable value

    Raises:
        ValueError: If required=True and variable is not set

    Example:
        >>> redis_url = get_env("REDIS_URL", required=True)
        >>> port = int(get_env("PORT", default="8000"))
    """
    value = os.getenv(key, default)

    if required and value is None:
        raise ValueError(f"Required environment variable '{key}' is not set")

    return value


def get_int_env(key: str, default: int) -> int:
    """
    Get integer environment variable.

    Args:
        key: Environment variable name
        default: Default value if not set or invalid

    Returns:
        Integer value
    """
    try:
        return int(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


def get_bool_env(key: str, default: bool = False) -> bool:
    """
    Get boolean environment variable.

    Accepts: true, 1, yes (case-insensitive) for True

    Args:
        key: Environment variable name
        default: Default value if not set

    Returns:
        Boolean value
    """
    value = os.getenv(key, "").lower()
    if not value:
        return default
    return value in ("true", "1", "yes")


class WorkerConfig:
    """
    Standard configuration for workers.

    Example:
        >>> config = WorkerConfig("embeddings")
        >>> print(config.queue_name)  # "embeddings"
        >>> print(config.prefetch_count)  # 5
    """

    def __init__(self, worker_name: str):
        self.worker_name = worker_name

        # Redis
        self.redis_url = get_env("REDIS_URL", required=True)

        # RabbitMQ
        self.rabbitmq_url = get_env("RABBITMQ_URL", required=True)
        self.queue_name = get_env("QUEUE_NAME", default=worker_name)
        self.prefetch_count = get_int_env("PREFETCH_COUNT", default=5)

        # Resource Manager
        self.resource_manager_url = get_env(
            "RESOURCE_MANAGER_URL", default="http://localhost:9090"
        )

        # Metrics
        self.metrics_port = get_int_env("METRICS_PORT", default=8000)

        # Logging
        self.log_level = get_env("LOG_LEVEL", default="INFO").upper()

        # Worker specific
        self.max_retries = get_int_env("MAX_RETRIES", default=3)
        self.retry_delay = get_int_env("RETRY_DELAY_SECONDS", default=5)

    def __repr__(self):
        return (
            f"WorkerConfig(worker_name='{self.worker_name}', queue='{self.queue_name}')"
        )
