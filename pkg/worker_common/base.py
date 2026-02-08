"""
BaseWorker class for IA Text Orchestrator Python workers.

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
from flask import Flask, jsonify

# Ensure pkg is in path
sys.path.insert(0, "/app")

from pkg.events_python import EventBus
from pkg.logging_python import setup_logging, JobLogger


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
        self._shutdown_requested = False

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
        self.logger.info(f"Received shutdown signal ({signum}), stopping worker...")
        self._shutdown_requested = True

    def _init_health_server(self) -> None:
        """Initialize Flask app for health checks."""
        self.app = Flask(__name__)

        @self.app.route("/health")
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

            # Check RabbitMQ (basic check)
            try:
                params = parse_rabbitmq_url(self.rabbitmq_url)
                connection = pika.BlockingConnection(params)
                connection.close()
                checks["rabbitmq"] = "ok"
            except Exception as e:
                checks["rabbitmq"] = f"error: {e}"
                status = "degraded"

            return jsonify(
                {
                    "status": status,
                    "worker": self.worker_name,
                    "checks": checks,
                    "metrics": "/metrics",
                }
            )

        @self.app.route("/metrics")
        def metrics():
            """Prometheus metrics endpoint."""
            from prometheus_client import CONTENT_TYPE_LATEST

            self.app.response_class.set(
                CONTENT_TYPE_LATEST, "text/plain; version=0.0.4; charset=utf-8"
            )
            return generate_latest()

        @self.app.route("/ready")
        def ready():
            """Readiness check - can accept work?"""
            if self._shutdown_requested:
                return jsonify({"ready": False, "reason": "shutdown_pending"}), 503
            return jsonify({"ready": True})

    @property
    def redis_client(self) -> redis.Redis:
        """Get Redis client, lazily initialized."""
        if self._redis_client is None:
            self._redis_client = redis.from_url(self.redis_url, decode_responses=True)
        return self._redis_client

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
        health_thread = threading.Thread(
            target=self.app.run,
            kwargs={
                "host": "0.0.0.0",
                "port": self.metrics_port + 1000,
                "debug": False,
            },
            daemon=True,
        )
        health_thread.start()
        self.logger.info(f"Health server started on port {self.metrics_port + 1000}")

        # Main processing loop
        while not self._shutdown_requested:
            try:
                with rabbitmq_connection(self.rabbitmq_url) as (connection, channel):
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
                self.logger.error(f"RabbitMQ connection error: {e}")
                if not self._shutdown_requested:
                    time.sleep(5)

        self.logger.info(f"{self.worker_name} shutdown complete")

    def _on_message(self, ch, method, properties, body) -> None:
        """
        Handle incoming message from RabbitMQ.

        Override this method in subclasses for custom processing.

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
            job_id = message.get("job_id")

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

        except Exception as e:
            duration = time.time() - start_time
            self.jobs_total.labels(status="error").inc()

            self.logger.error(
                f"Job {job_id} failed: {e}",
                extra={"job_id": job_id, "error": str(e)},
            )

            # Update job status in Redis
            if job_id:
                try:
                    self.redis_client.hset(
                        f"orchestrator:job:{job_id}:status",
                        mapping={"status": "error", "error": str(e)},
                    )
                    self.event_bus.publish_job_failed(job_id, str(e))
                except Exception as redis_error:
                    self.logger.error(f"Failed to update job status: {redis_error}")

            # Reject message (requeue if transient error)
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

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
