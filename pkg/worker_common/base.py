"""
BaseWorker class for textFlow Python workers.

Provides common functionality for all workers:
- Redis connection with automatic reconnection
- RabbitMQ connection with retry logic
- Signal handling for graceful shutdown
- Resource Manager client for GPU/resource checking
- Event Bus integration
- Prometheus metrics server
- Health check endpoints

Usage:
    from pkg.worker_common.base import BaseWorker

    class MyWorker(BaseWorker):
        def __init__(self, worker_name: str, queue_name: str):
            super().__init__(worker_name, queue_name)
            # Additional initialization

        def process_message(self, ch, method, properties, body):
            # Implement worker-specific logic
            pass

    if __name__ == "__main__":
        worker = MyWorker("my-worker", "my_queue")
        worker.run()
"""

import os
import sys
import json
import time
import signal
import logging
from typing import Callable, Dict, Optional, Any
from contextlib import contextmanager
from urllib.parse import urlparse

import pika
import redis
import requests
from prometheus_client import (
    Counter,
    Histogram,
    Gauge,
    start_http_server,
    generate_latest,
)
from fastapi import FastAPI
from fastapi.responses import JSONResponse

# Ensure pkg is in path
sys.path.insert(0, "/app")

from pkg.events_python import EventBus
from pkg.logging_python import setup_logging, JobLogger

MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "3"))


def parse_rabbitmq_url(url: str) -> pika.ConnectionParameters:
    """Parse AMQP URL and return ConnectionParameters.

    Supports URLs like: amqp://user:pass@host:port/vhost
    """
    parsed = urlparse(url)

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
    )


@contextmanager
def rabbitmq_connection(url: str, max_retries: int = 5):
    """Connect to RabbitMQ with retry logic."""
    logger = logging.getLogger("worker_common.rabbitmq")
    last_error = None

    for attempt in range(max_retries):
        try:
            params = parse_rabbitmq_url(url)
            connection = pika.BlockingConnection(params)
            channel = connection.channel()
            prefetch_count = int(os.getenv("PREFETCH_COUNT", "5"))
            channel.basic_qos(prefetch_count=prefetch_count)

            logger.info(
                f"Connected to RabbitMQ at {params.host}:{params.port} "
                f"with prefetch_count={prefetch_count}"
            )

            yield connection, channel
            return

        except Exception as e:
            last_error = e
            logger.warning(
                f"Failed to connect to RabbitMQ (attempt {attempt + 1}/{max_retries}): {e}"
            )
            if attempt < max_retries - 1:
                time.sleep(2**attempt)

    raise Exception(
        f"Failed to connect to RabbitMQ after {max_retries} retries: {last_error}"
    )


class ResourceManagerClient:
    """Client for communicating with the Resource Manager service."""

    def __init__(self, base_url: str, cache_ttl: int = 60):
        """
        Initialize Resource Manager client.

        Args:
            base_url: Resource Manager base URL
            cache_ttl: Cache TTL in seconds (default 60)
        """
        self.base_url = base_url
        self._cache: Optional[Dict] = None
        self._cache_time: float = 0
        self._cache_ttl = cache_ttl

    def get_resources(self) -> Dict:
        """Get available resources from Resource Manager."""
        now = time.time()
        if self._cache and (now - self._cache_time) < self._cache_ttl:
            return self._cache

        try:
            resp = requests.get(f"{self.base_url}/api/v1/resources", timeout=5)
            resp.raise_for_status()
            self._cache = resp.json()
            self._cache_time = now
            return self._cache
        except Exception as e:
            logger = logging.getLogger("worker_common.resource_manager")
            logger.warning(f"Failed to get resources from manager: {e}")
            return {"gpu_available": False}

    def acquire_resource(self, resource_type: str, worker_id: str) -> Optional[Dict]:
        """Acquire a resource (GPU, etc.) from Resource Manager."""
        try:
            resp = requests.post(
                f"{self.base_url}/api/v1/resources/acquire",
                json={"resource_type": resource_type, "worker_id": worker_id},
                timeout=5,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger = logging.getLogger("worker_common.resource_manager")
            logger.warning(f"Failed to acquire resource: {e}")
            return None

    def release_resource(self, resource_id: str) -> bool:
        """Release a resource back to Resource Manager."""
        try:
            resp = requests.post(
                f"{self.base_url}/api/v1/resources/release",
                json={"resource_id": resource_id},
                timeout=5,
            )
            resp.raise_for_status()
            return True
        except Exception as e:
            logger = logging.getLogger("worker_common.resource_manager")
            logger.warning(f"Failed to release resource {resource_id}: {e}")
            return False


class BaseWorker:
    """
    Base class for all Python workers.

    Provides:
    - Standardized logging with JobLogger
    - Redis connection
    - RabbitMQ connection with retry
    - Signal handling for graceful shutdown
    - Prometheus metrics
    - Health check endpoints
    - Event Bus integration
    - Resource Manager client
    """

    def __init__(
        self,
        worker_name: str,
        queue_name: str,
        metrics_port: int,
        requires_gpu: bool = False,
    ):
        """
        Initialize BaseWorker.

        Args:
            worker_name: Unique name for this worker
            queue_name: RabbitMQ queue to consume from
            metrics_port: Port for Prometheus metrics server
            requires_gpu: Whether this worker needs GPU resources
        """
        self.worker_name = worker_name
        self.queue_name = queue_name
        self.requires_gpu = requires_gpu
        self.metrics_port = metrics_port
        self.max_retries = MAX_RETRIES
        self._shutdown_requested = False
        self._stopping = False
        self._rabbitmq_connected = False

        # Setup logging
        self.logger = setup_logging(worker_name)
        self.job_logger = JobLogger(worker_name, self.logger)

        # Load environment
        self.redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
        self.rabbitmq_url = os.getenv("RABBITMQ_URL", "amqp://localhost:5672/")
        self.resource_manager_url = os.getenv(
            "RESOURCE_MANAGER_URL", "http://localhost:9090"
        )

        # Initialize connections (lazy)
        self._redis_client: Optional[redis.Redis] = None
        self._event_bus: Optional[EventBus] = None
        self._resource_manager: Optional[ResourceManagerClient] = None

        # Initialize metrics
        self._init_metrics()

        # Setup signal handlers
        self._setup_signal_handlers()

        # Flask app for health checks
        self._init_health_server()

    def _init_metrics(self) -> None:
        """Initialize Prometheus metrics."""
        self.jobs_total = Counter(
            f"{self.worker_name}_jobs_total",
            "Total jobs processed",
            ["status"],
        )
        self.job_duration = Histogram(
            f"{self.worker_name}_job_duration_seconds",
            "Job duration in seconds",
            buckets=[0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0],
        )
        self.gpu_available = Gauge(
            f"{self.worker_name}_gpu_available", "GPU availability", ["device"]
        )

    def _setup_signal_handlers(self) -> None:
        """Setup handlers for graceful shutdown."""
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame) -> None:
        """Handle shutdown signals."""
        self.logger.info(
            f"Received signal {signum}, initiating graceful shutdown..."
        )
        self._shutdown_requested = True
        self._stopping = True

    def _on_message_processed(self) -> None:
        """Call at the end of each message callback.

        Stops the RabbitMQ consumer if a graceful shutdown was requested.
        This ensures the worker finishes the current message before stopping,
        preventing jobs from being left in a permanent 'processing' state.
        """
        if self._stopping:
            self.logger.info(
                "Graceful shutdown: stopping consumer after current message"
            )
            if hasattr(self, "_channel") and self._channel and self._channel.is_open:
                self._channel.stop_consuming()

    def _init_health_server(self) -> None:
        """Initialize FastAPI app for health checks."""
        self.app = FastAPI(title=f"{self.worker_name}-health")

        @self.app.get("/health")
        def health():
            """Health check endpoint."""
            status = "healthy"
            checks = {}

            # Check Redis
            try:
                if self._redis_client:
                    self._redis_client.ping()
                    checks["redis"] = "ok"
                else:
                    checks["redis"] = "not_connected"
                    status = "degraded"
            except Exception as e:
                checks["redis"] = f"error: {e}"
                status = "unhealthy"

            # Check RabbitMQ (check cached status, don't create per-request connection)
            if self._rabbitmq_connected:
                checks["rabbitmq"] = "ok"
            else:
                checks["rabbitmq"] = "connecting"
                status = "degraded"

            return JSONResponse(
                {
                    "status": status,
                    "worker": self.worker_name,
                    "checks": checks,
                    "metrics": "/metrics",
                }
            )

        @self.app.get("/metrics")
        def metrics():
            """Prometheus metrics endpoint."""
            from prometheus_client import CONTENT_TYPE_LATEST

            return JSONResponse(
                generate_latest().decode("utf-8"),
                media_type="text/plain; version=0.0.4; charset=utf-8",
            )

        @self.app.get("/ready")
        def ready():
            """Readiness check - can accept work?"""
            if self._shutdown_requested:
                return JSONResponse(
                    {"ready": False, "reason": "shutdown_pending"}, status_code=503
                )
            return JSONResponse({"ready": True})

    @property
    def redis_client(self) -> redis.Redis:
        """Get Redis client with automatic reconnection on failure."""
        return self._get_redis()

    def _get_redis(self) -> redis.Redis:
        """Return Redis client, reconnecting if the connection is lost."""
        if self._redis_client is None:
            self._redis_client = self._connect_redis()
        else:
            try:
                self._redis_client.ping()
            except (redis.ConnectionError, redis.TimeoutError):
                self.logger.warning("Redis connection lost, reconnecting...")
                self._redis_client = self._connect_redis()
        return self._redis_client

    def _connect_redis(self) -> redis.Redis:
        """Create a new Redis connection with exponential backoff retry."""
        for attempt in range(1, 6):
            try:
                client = redis.from_url(self.redis_url, decode_responses=True)
                client.ping()
                return client
            except (redis.ConnectionError, redis.TimeoutError) as e:
                if attempt == 5:
                    raise
                wait = min(2 ** attempt, 30)
                self.logger.warning(
                    f"Redis connect attempt {attempt} failed: {e}. Retrying in {wait}s..."
                )
                time.sleep(wait)

    @property
    def event_bus(self) -> EventBus:
        """Get EventBus instance, lazily initialized."""
        if self._event_bus is None:
            self._event_bus = EventBus(self.redis_client)
        return self._event_bus

    @property
    def resource_manager(self) -> ResourceManagerClient:
        """Get Resource Manager client, lazily initialized."""
        if self._resource_manager is None:
            self._resource_manager = ResourceManagerClient(self.resource_manager_url)
        return self._resource_manager

    def get_resources(self) -> Dict:
        """Get available resources from Resource Manager."""
        return self.resource_manager.get_resources()

    def _publish_to_queue(
        self, queue: str, message: Dict, persistent: bool = True
    ) -> None:
        """Publish a message to a RabbitMQ queue."""
        with rabbitmq_connection(self.rabbitmq_url) as (connection, channel):
            channel.basic_publish(
                exchange="",
                routing_key=queue,
                body=json.dumps(message),
                properties=pika.BasicProperties(
                    delivery_mode=2 if persistent else 1,
                    content_type="application/json",
                ),
            )
            self.logger.info(
                f"Published to queue {queue}: {message.get('job_id', 'unknown')}"
            )

    def run(self) -> None:
        """Main worker loop - connect to RabbitMQ and process messages."""
        self.logger.info(f"Starting {self.worker_name}")
        self.logger.info(f"Queue: {self.queue_name}")

        # Start metrics server in background thread
        import threading

        metrics_thread = threading.Thread(
            target=start_http_server, args=(self.metrics_port,), daemon=True
        )
        metrics_thread.start()
        self.logger.info(f"Metrics server started on port {self.metrics_port}")

        # Start health check server in background thread
        health_port = self.metrics_port + 1000

        def start_uvicorn():
            import uvicorn

            uvicorn.run(
                self.app,
                host="0.0.0.0",
                port=health_port,
                log_level="warning",
            )

        health_thread = threading.Thread(
            target=start_uvicorn,
            daemon=True,
        )
        health_thread.start()
        self.logger.info(f"Health server started on port {health_port}")

        # Main processing loop
        while not self._shutdown_requested:
            try:
                with rabbitmq_connection(self.rabbitmq_url) as (connection, channel):
                    # Mark as connected
                    self._rabbitmq_connected = True

                    # Assign channel to instance so _on_message_processed()
                    # can call stop_consuming() on graceful shutdown.
                    self._channel = channel

                    # Declare queue
                    channel.queue_declare(queue=self.queue_name, durable=True)
                    self.logger.info(f"Consuming from queue: {self.queue_name}")

                    # Start consuming
                    channel.basic_consume(
                        queue=self.queue_name,
                        on_message_callback=self._on_message,
                        auto_ack=False,
                    )

                    channel.start_consuming()

            except Exception as e:
                self._rabbitmq_connected = False
                self.logger.error(f"RabbitMQ connection error: {e}")
                if not self._shutdown_requested:
                    time.sleep(5)

        self.logger.info(f"{self.worker_name} shutdown complete")

    def _get_retry_count(self, properties) -> int:
        """Extract retry count from message headers.

        First checks the custom ``x-retry-count`` header set by the delayed
        exchange retry path.  Falls back to summing ``x-death`` entries for
        backwards compatibility with the old DLX-based approach.

        Args:
            properties: RabbitMQ message properties (pika.BasicProperties)

        Returns:
            Retry count, or 0 if no retry headers are present.
        """
        if properties.headers:
            # Preferred: custom header written by delayed-exchange retry path
            if "x-retry-count" in properties.headers:
                return int(properties.headers["x-retry-count"])
            # Legacy: x-death headers from DLX-based retry
            if "x-death" in properties.headers:
                return sum(d.get("count", 0) for d in properties.headers["x-death"])
        return 0

    def _should_retry(self, properties) -> bool:
        """Return True if message should be requeued (under max retries).

        Checks x-death headers to determine how many times this message
        has already been dead-lettered. If under max_retries, returns True
        (requeue); otherwise False (send to DLQ permanently).

        Args:
            properties: RabbitMQ message properties (pika.BasicProperties)

        Returns:
            True if retry count < max_retries, False otherwise.
        """
        return self._get_retry_count(properties) < self.max_retries

    def _on_message(self, ch, method, properties, body) -> None:
        """
        Handle incoming message from RabbitMQ.

        Override process_message() in subclasses for custom processing.
        Transient errors (ConnectionError, TimeoutError) trigger automatic retry.
        Permanent errors (ValueError, etc.) are dead-lettered.

        Args:
            ch: RabbitMQ channel
            method: Delivery method
            properties: Message properties
            body: Message body
        """
        start_time = time.time()
        job_id = None

        try:
            message = json.loads(body)
            job_id = message.get("job_id") or message.get("id")

            # Call subclass method for actual processing
            result = self.process_message(message)

            # Update metrics
            duration = time.time() - start_time
            self.job_duration.observe(duration)
            self.jobs_total.labels(status="success").inc()

            # Acknowledge message
            ch.basic_ack(delivery_tag=method.delivery_tag)
            self.logger.info(
                f"Job {job_id} completed in {duration:.2f}s",
                extra={"job_id": job_id, "duration": duration},
            )

        except (ConnectionError, TimeoutError, redis.ConnectionError) as e:
            # Transient error — trigger automatic retry with exponential backoff
            duration = time.time() - start_time
            self.jobs_total.labels(status="transient_error").inc()

            self.logger.warning(
                f"Job {job_id} encountered transient error: {e}",
                extra={"job_id": job_id, "error": str(e)},
            )

            # Auto-retry with exponential backoff
            self._handle_transient_error(job_id, ch, method, properties, e, body)

        except Exception as e:
            # Permanent error — dead-letter the job
            duration = time.time() - start_time
            self.jobs_total.labels(status="error").inc()

            self.logger.error(
                f"Job {job_id} failed permanently: {e}",
                extra={"job_id": job_id, "error": str(e)},
            )

            # Update job status in Redis
            if job_id:
                try:
                    self.redis_client.set(
                        f"orchestrator:job:{job_id}:status",
                        "failed",
                    )
                    self.redis_client.set(
                        f"orchestrator:job:{job_id}:error",
                        str(e),
                    )
                    self.event_bus.publish_job_failed(job_id, str(e))
                except Exception as redis_error:
                    self.logger.error(f"Failed to update job status: {redis_error}")

            # Dead-letter the message (no requeue)
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

        finally:
            # Always check if shutdown was requested after processing each message.
            # This stops the consumer cleanly without interrupting ongoing work.
            self._on_message_processed()

    def _handle_transient_error(
        self,
        job_id: str,
        ch,
        method,
        properties,
        error: Exception,
        body: bytes = b"",
    ) -> None:
        """Handle transient error with delayed-exchange retry (non-blocking).

        Publishes the original message to ``document_processor_delayed`` with
        an ``x-delay`` header so RabbitMQ delivers it back to the original
        queue after the backoff period, without blocking the consumer thread.

        If the ``DELAYED_EXCHANGE_ENABLED`` env var is set to ``"false"`` (or
        the publish to the delayed exchange fails), falls back to the legacy
        ``basic_nack(requeue=True)`` with ``time.sleep()`` blocking behaviour.

        When max retries are exceeded, the message is dead-lettered by calling
        ``basic_nack(requeue=False)``.

        Args:
            job_id: Job ID for tracking retries.
            ch: RabbitMQ channel.
            method: Delivery method (routing_key, delivery_tag).
            properties: RabbitMQ message properties.
            error: The transient exception that occurred.
            body: Raw message body bytes (required for re-publish).
        """
        retry_count = self._get_retry_count(properties)

        if not self._should_retry(properties):
            # Max retries exceeded — dead-letter the job
            self.logger.warning(
                f"Message exceeded max retries ({self.max_retries}), sending to DLQ. "
                f"Job: {job_id}, Error: {error}"
            )
            self.jobs_total.labels(status="max_retries_exceeded").inc()

            if job_id:
                try:
                    self.redis_client.set(
                        f"orchestrator:job:{job_id}:status",
                        "failed",
                    )
                    self.redis_client.set(
                        f"orchestrator:job:{job_id}:error",
                        f"Max retries exceeded: {str(error)}",
                    )
                    self.event_bus.publish_job_failed(
                        job_id, f"Max retries exceeded: {str(error)}"
                    )
                except Exception as redis_error:
                    self.logger.error(f"Failed to update job status: {redis_error}")

            # Reject without requeue → goes to DLQ via DLX
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            return

        next_attempt = retry_count + 1
        backoff_ms = int(min(2**next_attempt, 60) * 1000)  # milliseconds for x-delay

        # Check whether delayed exchange is enabled (default: true)
        delayed_enabled = os.environ.get("DELAYED_EXCHANGE_ENABLED", "true").lower() != "false"

        if delayed_enabled:
            self._retry_via_delayed_exchange(
                job_id, ch, method, properties, error, body, next_attempt, backoff_ms
            )
        else:
            self._retry_via_sleep(job_id, ch, method, properties, error, next_attempt, backoff_ms // 1000)

    def _retry_via_delayed_exchange(
        self,
        job_id: str,
        ch,
        method,
        properties,
        error: Exception,
        body: bytes,
        next_attempt: int,
        backoff_ms: int,
    ) -> None:
        """Publish to delayed exchange; falls back to sleep+nack on failure."""
        original_queue = method.routing_key

        self.logger.info(
            f"Job {job_id} scheduled for retry in {backoff_ms}ms via delayed exchange "
            f"(attempt {next_attempt}/{self.max_retries})"
        )

        # Build new headers: propagate existing headers, set retry tracking
        headers = dict(properties.headers or {})
        headers["x-retry-count"] = next_attempt
        headers["x-delay"] = backoff_ms
        # Remove x-death to avoid confusion (we track retries via x-retry-count)
        headers.pop("x-death", None)

        try:
            ch.basic_publish(
                exchange="document_processor_delayed",
                routing_key=original_queue,
                body=body,
                properties=pika.BasicProperties(
                    delivery_mode=2,  # persistent
                    headers=headers,
                    content_type=properties.content_type,
                ),
            )
            # ACK the original message — we have re-published to the delayed queue
            ch.basic_ack(delivery_tag=method.delivery_tag)
        except Exception as pub_err:
            self.logger.error(
                f"Failed to publish to delayed exchange: {pub_err}; "
                f"falling back to blocking requeue"
            )
            backoff_seconds = backoff_ms // 1000
            self._retry_via_sleep(
                job_id, ch, method, properties, pub_err, next_attempt, backoff_seconds
            )

    def _retry_via_sleep(
        self,
        job_id: str,
        ch,
        method,
        properties,
        error: Exception,
        next_attempt: int,
        backoff_seconds: int,
    ) -> None:
        """Legacy blocking retry: sleep then basic_nack(requeue=True)."""
        self.logger.info(
            f"Job {job_id} will retry in {backoff_seconds}s "
            f"(attempt {next_attempt}/{self.max_retries}) [blocking fallback]"
        )
        # NOTE: This blocks the consumer thread for up to 60 seconds per retry.
        # Use DELAYED_EXCHANGE_ENABLED=true (default) to avoid this.
        time.sleep(backoff_seconds)
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)

    def _extract_job_id(self, body: bytes) -> str | None:
        """Extract job_id from message body JSON without raising.

        Tries 'job_id' first, then falls back to 'id'. Returns None if the
        body is not valid JSON or contains neither field.

        Args:
            body: Raw message body bytes

        Returns:
            Job ID string or None
        """
        try:
            data = json.loads(body)
            return data.get("job_id") or data.get("id") or None
        except Exception:
            return None

    def process_message(self, message: Dict) -> Any:
        """
        Process a single message.

        Override this method in subclasses to implement custom processing logic.

        Args:
            message: Parsed message dictionary from RabbitMQ

        Returns:
            Processing result (any type)
        """
        raise NotImplementedError("Subclasses must implement process_message")

    def should_shutdown(self) -> bool:
        """Check if shutdown has been requested."""
        return self._shutdown_requested


# Legacy support: handle_retry function for workers still using direct function calls
def handle_retry(
    job_id: str,
    queue_name: str,
    redis_client=None,
    max_retries: int = 3,
    backoff_seconds: int = None,
) -> bool:
    """
    Legacy retry handler for direct function calls (for backward compatibility).

    New code should inherit from BaseWorker which provides automatic
    transient error detection and retry via _handle_transient_error().

    This function is kept for compatibility with existing worker code.

    Args:
        job_id: Job ID for tracking retries
        queue_name: RabbitMQ queue name
        redis_client: Redis client instance (optional, will use default if not provided)
        max_retries: Maximum number of retries (default 3)
        backoff_seconds: Custom backoff duration in seconds (optional)

    Returns:
        True if should retry, False if exceeded max retries
    """
    if not redis_client:
        try:
            import redis

            redis_client = redis.Redis.from_url(
                os.environ.get("REDIS_URL", "redis://localhost:6379")
            )
        except Exception:
            logger.warning("Could not create redis client for handle_retry")
            return False

    retry_key = f"orchestrator:job:{job_id}:retry:{queue_name}"
    retry_count = 0

    try:
        retry_count = int(redis_client.get(retry_key) or 0)
    except Exception:
        retry_count = 0

    if retry_count >= max_retries:
        logger.error(
            f"Job {job_id} exceeded max retries ({max_retries}) on queue {queue_name}"
        )
        return False

    retry_count += 1
    redis_client.setex(retry_key, 3600, str(retry_count))  # 1 hour TTL

    if backoff_seconds:
        logger.info(
            f"Retrying job {job_id} (attempt {retry_count}) after {backoff_seconds}s"
        )
        time.sleep(backoff_seconds)
    else:
        logger.info(f"Retrying job {job_id} (attempt {retry_count})")

    return True
