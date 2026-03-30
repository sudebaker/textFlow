"""Async RabbitMQ connection helpers using aio_pika.

Parallel to rabbitmq.py (sync/pika), this module provides async equivalents
for workers that use asyncio + aio_pika.
"""

import asyncio
import logging
import os
from typing import Optional

import aio_pika
import aio_pika.abc

logger = logging.getLogger(__name__)

# Dead Letter Exchange config — must match internal/broker/rabbitmq.go
DLX_EXCHANGE = "document_processor_dlx"


async def connect_rabbitmq_async(
    url: str,
    max_retries: int = 5,
    prefetch_count: int = 10,
) -> aio_pika.RobustConnection:
    """Connect to RabbitMQ with retry logic using aio_pika.

    Args:
        url: RabbitMQ connection URL (e.g., amqp://user:pass@host:5672/).
        max_retries: Maximum connection attempts before raising.
        prefetch_count: QoS prefetch count (set on channel after connecting).

    Returns:
        An open aio_pika.RobustConnection.

    Raises:
        RuntimeError: If all connection attempts fail.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            connection = await aio_pika.connect_robust(url)
            logger.info(f"Connected to RabbitMQ (attempt {attempt + 1})")
            return connection
        except Exception as exc:
            last_exc = exc
            wait = 2 ** attempt
            logger.warning(
                f"RabbitMQ connect attempt {attempt + 1}/{max_retries} failed: {exc}. "
                f"Retrying in {wait}s"
            )
            await asyncio.sleep(wait)
    raise RuntimeError(
        f"Failed to connect to RabbitMQ after {max_retries} retries: {last_exc}"
    )


async def declare_queue_async(
    channel: aio_pika.abc.AbstractChannel,
    queue_name: str,
    durable: bool = True,
) -> aio_pika.abc.AbstractQueue:
    """Declare a durable queue with DLX arguments matching the Go orchestrator.

    This is idempotent — safe to call from both workers and orchestrator.
    The DLX args must match internal/broker/rabbitmq.go:declareQueue().

    Args:
        channel: An open aio_pika channel.
        queue_name: Name of the queue to declare.
        durable: Whether the queue survives broker restarts.

    Returns:
        The declared queue object.
    """
    return await channel.declare_queue(
        queue_name,
        durable=durable,
        arguments={
            "x-dead-letter-exchange": DLX_EXCHANGE,
            "x-dead-letter-routing-key": f"{queue_name}_failed",
        },
    )
