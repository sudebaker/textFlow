"""
Test fixtures and utilities for Python workers.

Provides shared fixtures for testing workers, including:
- Mock Redis client
- Mock RabbitMQ channel
- Test job factories
- Assertion helpers

Usage:
    from pkg.test_fixtures import MockRedis, MockRabbitMQ, TestJob

    def test_my_worker():
        redis = MockRedis()
        worker = MyWorker(redis_client=redis)
        result = worker.process({"job_id": "test-123"})
        assert result.status == "completed"
"""

import json
import time
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch


class MockRedis:
    """Mock Redis client for testing."""

    def __init__(self):
        self.data = {}
        self.hashes = {}
        self.expiry = {}

    def get(self, key: str) -> Optional[str]:
        """Get a value from Redis."""
        # Check expiry
        if key in self.expiry and time.time() > self.expiry[key]:
            del self.data[key]
            del self.expiry[key]
            return None
        return self.data.get(key)

    def set(self, key: str, value: str, ex: Optional[int] = None) -> bool:
        """Set a value in Redis."""
        self.data[key] = value
        if ex:
            self.expiry[key] = time.time() + ex
        return True

    def delete(self, *keys: str) -> int:
        """Delete keys from Redis."""
        count = 0
        for key in keys:
            if key in self.data:
                del self.data[key]
                count += 1
            if key in self.expiry:
                del self.expiry[key]
        return count

    def hget(self, key: str, field: str) -> Optional[str]:
        """Get a field from a hash."""
        if key not in self.hashes:
            return None
        return self.hashes[key].get(field)

    def hset(self, key: str, field: str, value: str) -> int:
        """Set a field in a hash."""
        if key not in self.hashes:
            self.hashes[key] = {}
        self.hashes[key][field] = value
        return 1

    def hgetall(self, key: str) -> Dict[str, str]:
        """Get all fields from a hash."""
        if key not in self.hashes:
            return {}
        return self.hashes[key].copy()

    def exists(self, key: str) -> int:
        """Check if a key exists."""
        return 1 if key in self.data else 0

    def ping(self) -> bool:
        """Ping Redis."""
        return True

    def flushall(self):
        """Clear all data."""
        self.data = {}
        self.hashes = {}
        self.expiry = {}


class MockRabbitMQChannel:
    """Mock RabbitMQ channel for testing."""

    def __init__(self):
        self.messages = []
        self.queues = {}
        self.consumer_tags = []

    def basic_publish(
        self,
        exchange: str = "",
        routing_key: str = "",
        body: bytes = b"",
        properties=None,
    ):
        """Publish a message to a queue."""
        self.messages.append(
            {
                "exchange": exchange,
                "routing_key": routing_key,
                "body": body,
                "properties": properties,
            }
        )

    def queue_declare(self, queue: str, durable: bool = True) -> Dict[str, int]:
        """Declare a queue."""
        self.queues[queue] = {
            "message_count": len(
                [m for m in self.messages if m["routing_key"] == queue]
            ),
            "consumer_count": 0,
        }

    def basic_qos(self, prefetch_count: int = 0):
        """Set QoS."""
        pass

    def basic_consume(
        self,
        queue: str = "",
        on_message_callback=None,
        auto_ack: bool = False,
    ):
        """Start consuming from a queue."""
        pass

    def basic_ack(self, delivery_tag: int):
        """Acknowledge a message."""
        pass

    def basic_nack(self, delivery_tag: int, requeue: bool = False):
        """Negative acknowledge a message."""
        pass


class MockRabbitMQConnection:
    """Mock RabbitMQ connection for testing."""

    def __init__(self):
        self.channel = MockRabbitMQChannel()

    def channel(self) -> MockRabbitMQChannel:
        """Get the channel."""
        return self.channel

    def close(self):
        """Close the connection."""
        pass


@dataclass
class TestJob:
    """Test job fixture for testing workers."""

    job_id: str
    status: str = "pending"
    text: Optional[str] = None
    embeddings: Optional[List[float]] = None
    entities: Optional[List[Dict[str, Any]]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    steps: Dict[str, str] = field(default_factory=dict)
    error: Optional[str] = None

    def with_status(self, status: str) -> "TestJob":
        """Set the job status."""
        self.status = status
        return self

    def with_text(self, text: str) -> "TestJob":
        """Set the extracted text."""
        self.text = text
        return self

    def with_embeddings(self, embeddings: List[float]) -> "TestJob":
        """Set the embeddings."""
        self.embeddings = embeddings
        return self

    def with_entity(self, entity: Dict[str, Any]) -> "TestJob":
        """Add an entity."""
        if self.entities is None:
            self.entities = []
        self.entities.append(entity)
        return self

    def with_metadata(self, key: str, value: Any) -> "TestJob":
        """Set metadata."""
        self.metadata[key] = value
        return self

    def with_step(self, step: str, status: str) -> "TestJob":
        """Set a step status."""
        self.steps[step] = status
        return self

    def with_error(self, error: str) -> "TestJob":
        """Set an error."""
        self.error = error
        return self

    def build(self, redis: MockRedis) -> "TestJob":
        """Build the job and store it in Redis."""
        # Set status
        redis.set(f"orchestrator:job:{self.job_id}:status", self.status)

        # Set text
        if self.text:
            redis.set(f"orchestrator:job:{self.job_id}:text", self.text)

        # Set embeddings
        if self.embeddings:
            redis.set(
                f"orchestrator:job:{self.job_id}:embeddings",
                json.dumps(self.embeddings),
            )

        # Set entities
        if self.entities:
            redis.set(
                f"orchestrator:job:{self.job_id}:entities", json.dumps(self.entities)
            )

        # Set metadata
        for k, v in self.metadata.items():
            redis.hset(f"orchestrator:job:{self.job_id}:meta", k, str(v))

        # Set steps
        for step, status in self.steps.items():
            redis.hset(f"orchestrator:job:{self.job_id}:steps", step, status)

        # Set error
        if self.error:
            redis.set(f"orchestrator:job:{self.job_id}:error", self.error)

        return self

    def to_message(self) -> Dict[str, Any]:
        """Convert to RabbitMQ message format."""
        message = {"job_id": self.job_id}
        if self.text:
            message["text"] = self.text
        return message


class AssertionHelpers:
    """Assertion helpers for testing."""

    @staticmethod
    def assert_job_status(redis: MockRedis, job_id: str, expected_status: str):
        """Assert job status matches expected."""
        actual = redis.get(f"orchestrator:job:{job_id}:status")
        assert actual == expected_status, (
            f"Expected status {expected_status}, got {actual}"
        )

    @staticmethod
    def assert_job_exists(redis: MockRedis, job_id: str):
        """Assert job exists in Redis."""
        exists = redis.exists(f"orchestrator:job:{job_id}:status")
        assert exists == 1, f"Job {job_id} should exist"

    @staticmethod
    def assert_job_not_exists(redis: MockRedis, job_id: str):
        """Assert job does not exist in Redis."""
        exists = redis.exists(f"orchestrator:job:{job_id}:status")
        assert exists == 0, f"Job {job_id} should not exist"

    @staticmethod
    def assert_step_completed(redis: MockRedis, job_id: str, step: str):
        """Assert step is completed."""
        status = redis.hget(f"orchestrator:job:{job_id}:steps", step)
        assert status == "completed", f"Step {step} should be completed"

    @staticmethod
    def assert_contains_text(redis: MockRedis, job_id: str, expected: str):
        """Assert extracted text contains expected substring."""
        text = redis.get(f"orchestrator:job:{job_id}:text")
        assert expected in text, f"Text should contain '{expected}'"

    @staticmethod
    def assert_embeddings_stored(redis: MockRedis, job_id: str):
        """Assert embeddings are stored."""
        embeddings = redis.get(f"orchestrator:job:{job_id}:embeddings")
        assert embeddings is not None, "Embeddings should be stored"
        # Verify it's valid JSON
        parsed = json.loads(embeddings)
        assert isinstance(parsed, list), "Embeddings should be a list"


class MetricsCollector:
    """Mock metrics collector for testing."""

    def __init__(self):
        self.counters = {}
        self.histograms = {}
        self.gauges = {}

    def counter(self, name: str, labels: Dict[str, str] = None) -> int:
        """Get counter value."""
        key = f"{name}:{labels or {}}"
        return self.counters.get(key, 0)

    def increment(self, name: str, labels: Dict[str, str] = None):
        """Increment a counter."""
        key = f"{name}:{str(labels or {})}"
        self.counters[key] = self.counters.get(key, 0) + 1

    def gauge(self, name: str, value: float, labels: Dict[str, str] = None):
        """Set a gauge value."""
        key = f"{name}:{str(labels or {})}"
        self.gauges[key] = value

    def observe(self, name: str, value: float, labels: Dict[str, str] = None):
        """Observe a histogram value."""
        key = f"{name}:{str(labels or {})}"
        if key not in self.histograms:
            self.histograms[key] = []
        self.histograms[key].append(value)


def create_mock_worker_config(
    worker_name: str = "test-worker",
    queue_name: str = "test_queue",
    metrics_port: int = 8001,
) -> Dict[str, Any]:
    """Create a mock worker configuration for testing."""
    return {
        "worker_name": worker_name,
        "queue_name": queue_name,
        "metrics_port": metrics_port,
        "redis_url": "redis://localhost:6379",
        "rabbitmq_url": "amqp://guest:guest@localhost:5672/",
        "resource_manager_url": "http://localhost:9090",
        "prefetch_count": 5,
    }


def patch_redis_client():
    """Patch Redis client for testing."""
    return patch("redis.from_url", return_value=MockRedis())


def patch_rabbitmq_connection():
    """Patch RabbitMQ connection for testing."""
    return patch("pika.BlockingConnection", return_value=MockRabbitMQConnection())


def patch_event_bus():
    """Patch EventBus for testing."""
    mock = MagicMock()
    return mock


# Example test using fixtures
def example_test():
    """Example test showing how to use fixtures."""
    # Setup
    redis = MockRedis()
    job = TestJob("test-123").with_text("Hello world")
    job.build(redis)

    # Assert
    AssertionHelpers.assert_job_exists(redis, "test-123")
    AssertionHelpers.assert_contains_text(redis, "test-123", "Hello world")

    # Metrics
    metrics = MetricsCollector()
    metrics.increment("jobs_total", {"status": "success"})
    assert metrics.counter("jobs_total") == 1

    print("✅ Test passed!")
