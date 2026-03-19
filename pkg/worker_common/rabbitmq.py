"""
RabbitMQ connection utilities for workers.
"""

import logging
import os
import time
import urllib.parse
from contextlib import contextmanager
from typing import Generator, Optional, Tuple

import pika

logger = logging.getLogger(__name__)

# Dead Letter Exchange config — must match internal/broker/rabbitmq.go
DLX_EXCHANGE = "document_processor_dlx"


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


def declare_queue(channel, queue_name: str, durable: bool = True) -> None:
    """Declare a durable queue with DLX arguments matching the Go orchestrator.

    This is idempotent — safe to call from both workers and orchestrator.
    The DLX args must match internal/broker/rabbitmq.go:declareQueue().

    Args:
        channel: An open pika channel.
        queue_name: Name of the queue to declare.
        durable: Whether the queue survives broker restarts.
    """
    channel.queue_declare(
        queue=queue_name,
        durable=durable,
        arguments={
            "x-dead-letter-exchange": DLX_EXCHANGE,
            "x-dead-letter-routing-key": f"{queue_name}_failed",
        },
    )


@contextmanager
def connect_rabbitmq(
    url: str,
    max_retries: int = 5,
    prefetch_count: Optional[int] = None,
) -> Generator[
    Tuple[pika.BlockingConnection, pika.adapters.blocking_connection.BlockingChannel],
    None,
    None,
]:
    """Connect to RabbitMQ with retry logic for the initial connection only.

    Yields (connection, channel). The retry loop only covers the initial
    connection attempt — exceptions raised inside the caller's ``with`` block
    propagate normally so that the outer ``while True`` reconnection loop can
    handle them.

    Args:
        url: RabbitMQ connection URL.
        max_retries: Maximum number of initial connection attempts.
        prefetch_count: QoS prefetch count. Defaults to PREFETCH_COUNT env var
            or 5 if unset.

    Yields:
        Tuple of (connection, channel).

    Raises:
        Exception: If the initial connection fails after max_retries.
    """
    if prefetch_count is None:
        prefetch_count = int(os.getenv("PREFETCH_COUNT", "5"))

    connection = None
    params = None
    for attempt in range(max_retries):
        try:
            params = parse_rabbitmq_url(url)
            connection = pika.BlockingConnection(params)
            break
        except Exception as e:
            logger.warning(
                f"Failed to connect to RabbitMQ "
                f"(attempt {attempt + 1}/{max_retries}): {e}"
            )
            if attempt < max_retries - 1:
                time.sleep(2**attempt)

    if connection is None:
        raise Exception(f"Failed to connect to RabbitMQ after {max_retries} retries")

    channel = connection.channel()
    channel.basic_qos(prefetch_count=prefetch_count)
    logger.info(
        f"Connected to RabbitMQ at {params.host}:{params.port} "
        f"with prefetch_count={prefetch_count}"
    )

    try:
        yield connection, channel
    finally:
        try:
            connection.close()
        except Exception:
            pass


# Backward compatibility alias — BaseWorker imports rabbitmq_connection
rabbitmq_connection = connect_rabbitmq
