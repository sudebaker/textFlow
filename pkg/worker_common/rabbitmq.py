"""
RabbitMQ connection utilities for workers.
"""

import logging
import time
import urllib.parse
from contextlib import contextmanager
from typing import Generator, Tuple

import pika

logger = logging.getLogger(__name__)


def parse_rabbitmq_url(url: str) -> pika.ConnectionParameters:
    """
    Parse RabbitMQ URL and return ConnectionParameters.

    Args:
        url: RabbitMQ connection URL (e.g., amqp://user:pass@host:5672/)

    Returns:
        pika.ConnectionParameters configured from URL

    Example:
        >>> params = parse_rabbitmq_url("amqp://guest:guest@localhost:5672/")
        >>> connection = pika.BlockingConnection(params)
    """
    parsed = urllib.parse.urlparse(url)
    credentials = pika.PlainCredentials(
        parsed.username or "guest", parsed.password or "guest"
    )

    return pika.ConnectionParameters(
        host=parsed.hostname or "localhost",
        port=parsed.port or 5672,
        virtual_host=parsed.path[1:] if parsed.path else "/",
        credentials=credentials,
        heartbeat=600,
        blocked_connection_timeout=300,
        frame_max=131072,  # Increase frame size for large messages
        connection_attempts=3,
        retry_delay=2,
    )


@contextmanager
def rabbitmq_connection(
    url: str, max_retries: int = 5
) -> Generator[
    Tuple[pika.BlockingConnection, pika.adapters.blocking_connection.BlockingChannel],
    None,
    None,
]:
    """
    Context manager for RabbitMQ connection with retry logic.

    Args:
        url: RabbitMQ connection URL
        max_retries: Maximum number of connection attempts

    Yields:
        Tuple of (connection, channel)

    Raises:
        Exception: If connection fails after max_retries

    Example:
        >>> with rabbitmq_connection(RABBITMQ_URL) as (conn, channel):
        >>>     channel.basic_qos(prefetch_count=5)
        >>>     # Use channel...
    """
    for attempt in range(max_retries):
        try:
            params = parse_rabbitmq_url(url)
            connection = pika.BlockingConnection(params)
            channel = connection.channel()
            logger.info(f"Connected to RabbitMQ at {params.host}:{params.port}")
            yield connection, channel
            return
        except Exception as e:
            logger.warning(
                f"Failed to connect to RabbitMQ (attempt {attempt + 1}/{max_retries}): {e}"
            )
            if attempt < max_retries - 1:
                time.sleep(2**attempt)
    raise Exception("Failed to connect to RabbitMQ after max retries")
