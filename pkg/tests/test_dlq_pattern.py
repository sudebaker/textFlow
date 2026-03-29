"""
Tests for Dead Letter Queue (DLQ) pattern in BaseWorker.

RED phase: These tests verify that:
1. _get_retry_count correctly reads x-death headers
2. _should_retry returns True/False based on retry count vs max_retries
3. basic_nack is called with requeue=False when max_retries is exceeded
4. basic_nack is called with requeue=True when under max_retries
"""

import json
from unittest.mock import MagicMock, patch, call


def _make_worker(max_retries=3):
    """Create a minimal BaseWorker instance without real connections."""
    with patch("pkg.worker_common.base.setup_logging", return_value=MagicMock()):
        with patch("pkg.worker_common.base.JobLogger", return_value=MagicMock()):
            with patch("pkg.worker_common.base.FastAPI", return_value=MagicMock()):
                with patch("pkg.worker_common.base.EventBus", return_value=MagicMock()):
                    with patch("pkg.worker_common.base.Counter", return_value=MagicMock()):
                        with patch("pkg.worker_common.base.Histogram", return_value=MagicMock()):
                            with patch("pkg.worker_common.base.Gauge", return_value=MagicMock()):
                                from pkg.worker_common.base import BaseWorker

                                class TestWorker(BaseWorker):
                                    def process_message(self, message):
                                        return "ok"

                                worker = TestWorker(
                                    worker_name="test-worker",
                                    queue_name="test_queue",
                                    metrics_port=9998,
                                )
                                worker.max_retries = max_retries
                                return worker


def _make_failing_worker(max_retries=3):
    """Create a BaseWorker whose process_message always raises an exception."""
    with patch("pkg.worker_common.base.setup_logging", return_value=MagicMock()):
        with patch("pkg.worker_common.base.JobLogger", return_value=MagicMock()):
            with patch("pkg.worker_common.base.FastAPI", return_value=MagicMock()):
                with patch("pkg.worker_common.base.EventBus", return_value=MagicMock()):
                    with patch("pkg.worker_common.base.Counter", return_value=MagicMock()):
                        with patch("pkg.worker_common.base.Histogram", return_value=MagicMock()):
                            with patch("pkg.worker_common.base.Gauge", return_value=MagicMock()):
                                from pkg.worker_common.base import BaseWorker

                                class FailingWorker(BaseWorker):
                                    def process_message(self, message):
                                        raise ConnectionError("Simulated transient failure")

                                worker = FailingWorker(
                                    worker_name="test-worker-fail",
                                    queue_name="test_queue",
                                    metrics_port=9997,
                                )
                                worker.max_retries = max_retries
                                return worker


class TestGetRetryCount:
    """Tests for _get_retry_count method."""

    def test_get_retry_count_returns_zero_when_headers_is_none(self):
        """_get_retry_count must return 0 when properties.headers is None."""
        worker = _make_worker()
        props = MagicMock()
        props.headers = None

        assert worker._get_retry_count(props) == 0

    def test_get_retry_count_returns_zero_when_no_xdeath_key(self):
        """_get_retry_count must return 0 when headers dict has no x-death key."""
        worker = _make_worker()
        props = MagicMock()
        props.headers = {"content-type": "application/json"}

        assert worker._get_retry_count(props) == 0

    def test_get_retry_count_returns_zero_when_xdeath_is_empty_list(self):
        """_get_retry_count must return 0 when x-death is an empty list."""
        worker = _make_worker()
        props = MagicMock()
        props.headers = {"x-death": []}

        assert worker._get_retry_count(props) == 0

    def test_get_retry_count_returns_count_from_single_xdeath_entry(self):
        """_get_retry_count must sum counts from x-death entries (single entry)."""
        worker = _make_worker()
        props = MagicMock()
        props.headers = {"x-death": [{"count": 2, "queue": "test_queue"}]}

        assert worker._get_retry_count(props) == 2

    def test_get_retry_count_sums_multiple_xdeath_entries(self):
        """_get_retry_count must sum counts across multiple x-death entries."""
        worker = _make_worker()
        props = MagicMock()
        # RabbitMQ accumulates x-death entries from different queues/exchanges
        props.headers = {
            "x-death": [
                {"count": 2, "queue": "test_queue"},
                {"count": 1, "queue": "dlq"},
            ]
        }

        assert worker._get_retry_count(props) == 3

    def test_get_retry_count_handles_missing_count_key_in_entry(self):
        """_get_retry_count must handle x-death entries without 'count' key gracefully."""
        worker = _make_worker()
        props = MagicMock()
        props.headers = {"x-death": [{"queue": "test_queue"}]}  # no "count" key

        assert worker._get_retry_count(props) == 0


class TestShouldRetry:
    """Tests for _should_retry method."""

    def test_should_retry_returns_true_when_no_xdeath_headers(self):
        """_should_retry returns True when there are no x-death headers (first attempt)."""
        worker = _make_worker(max_retries=3)
        props = MagicMock()
        props.headers = None

        assert worker._should_retry(props) is True

    def test_should_retry_returns_true_when_under_max_retries(self):
        """_should_retry returns True when retry count < max_retries."""
        worker = _make_worker(max_retries=3)
        props = MagicMock()
        props.headers = {"x-death": [{"count": 2}]}  # 2 < 3

        assert worker._should_retry(props) is True

    def test_should_retry_returns_false_when_at_max_retries(self):
        """_should_retry returns False when retry count == max_retries."""
        worker = _make_worker(max_retries=3)
        props = MagicMock()
        props.headers = {"x-death": [{"count": 3}]}  # 3 == 3

        assert worker._should_retry(props) is False

    def test_should_retry_returns_false_when_over_max_retries(self):
        """_should_retry returns False when retry count > max_retries."""
        worker = _make_worker(max_retries=3)
        props = MagicMock()
        props.headers = {"x-death": [{"count": 5}]}  # 5 > 3

        assert worker._should_retry(props) is False

    def test_should_retry_respects_custom_max_retries(self):
        """_should_retry respects the worker's max_retries setting."""
        worker = _make_worker(max_retries=5)
        props = MagicMock()
        props.headers = {"x-death": [{"count": 4}]}  # 4 < 5

        assert worker._should_retry(props) is True


class TestMaxRetriesAttribute:
    """Tests that BaseWorker initializes max_retries from environment."""

    def test_worker_has_max_retries_attribute(self):
        """BaseWorker instances must have a max_retries attribute."""
        worker = _make_worker()
        assert hasattr(worker, "max_retries")

    def test_max_retries_default_is_three(self):
        """Default max_retries must be 3 when MAX_RETRIES env var is not set."""
        import os
        # Ensure env var is not set
        os.environ.pop("MAX_RETRIES", None)

        worker = _make_worker()
        # Reset to default (worker factory sets max_retries directly,
        # but we verify the class default via fresh import)
        with patch("pkg.worker_common.base.setup_logging", return_value=MagicMock()):
            with patch("pkg.worker_common.base.JobLogger", return_value=MagicMock()):
                with patch("pkg.worker_common.base.FastAPI", return_value=MagicMock()):
                    with patch("pkg.worker_common.base.EventBus", return_value=MagicMock()):
                        with patch("pkg.worker_common.base.Counter", return_value=MagicMock()):
                            with patch("pkg.worker_common.base.Histogram", return_value=MagicMock()):
                                with patch("pkg.worker_common.base.Gauge", return_value=MagicMock()):
                                    from pkg.worker_common.base import BaseWorker

                                    class TestWorker(BaseWorker):
                                        def process_message(self, message):
                                            return None

                                    w = TestWorker("t", "q", 9996)
                                    assert w.max_retries == 3

    def test_max_retries_reads_from_env(self):
        """max_retries must read from MAX_RETRIES environment variable."""
        import os
        os.environ["MAX_RETRIES"] = "5"
        try:
            with patch("pkg.worker_common.base.setup_logging", return_value=MagicMock()):
                with patch("pkg.worker_common.base.JobLogger", return_value=MagicMock()):
                    with patch("pkg.worker_common.base.FastAPI", return_value=MagicMock()):
                        with patch("pkg.worker_common.base.EventBus", return_value=MagicMock()):
                            with patch("pkg.worker_common.base.Counter", return_value=MagicMock()):
                                with patch("pkg.worker_common.base.Histogram", return_value=MagicMock()):
                                    with patch("pkg.worker_common.base.Gauge", return_value=MagicMock()):
                                        # Re-import base to pick up new env var
                                        import importlib
                                        import pkg.worker_common.base as base_module
                                        importlib.reload(base_module)

                                        class TestWorker(base_module.BaseWorker):
                                            def process_message(self, message):
                                                return None

                                        w = TestWorker("t", "q", 9995)
                                        assert w.max_retries == 5
        finally:
            os.environ.pop("MAX_RETRIES", None)
            # Reload to restore default
            import importlib
            import pkg.worker_common.base as base_module
            importlib.reload(base_module)


class TestTransientErrorNackBehavior:
    """Tests that transient errors use _should_retry for nack requeue flag."""

    def _make_props_with_xdeath(self, count):
        """Helper: create mock properties with x-death count."""
        props = MagicMock()
        if count is None:
            props.headers = None
        else:
            props.headers = {"x-death": [{"count": count}]}
        return props

    def test_nack_requeue_true_when_under_max_retries(self):
        """basic_nack must be called with requeue=True when retry count < max_retries."""
        worker = _make_failing_worker(max_retries=3)

        # Mock Redis to avoid connection
        mock_redis = MagicMock()
        mock_redis.get.return_value = None
        mock_redis.set = MagicMock()
        mock_redis.setex = MagicMock()
        worker._redis_client = mock_redis

        # Mock event_bus
        worker._event_bus = MagicMock()

        ch = MagicMock()
        method = MagicMock()
        method.delivery_tag = "tag-123"

        props = self._make_props_with_xdeath(1)  # 1 < 3 → should retry

        body = json.dumps({"job_id": "job-test-1"}).encode()

        worker._on_message(ch, method, props, body)

        # basic_nack must have been called with requeue=True
        ch.basic_nack.assert_called_once_with(
            delivery_tag="tag-123", requeue=True
        )

    def test_nack_requeue_false_when_max_retries_exceeded(self):
        """basic_nack must be called with requeue=False when retry count >= max_retries."""
        worker = _make_failing_worker(max_retries=3)

        # Mock Redis to avoid connection
        mock_redis = MagicMock()
        mock_redis.get.return_value = None
        mock_redis.set = MagicMock()
        mock_redis.setex = MagicMock()
        worker._redis_client = mock_redis

        # Mock event_bus
        worker._event_bus = MagicMock()

        ch = MagicMock()
        method = MagicMock()
        method.delivery_tag = "tag-456"

        props = self._make_props_with_xdeath(3)  # 3 == 3 → send to DLQ

        body = json.dumps({"job_id": "job-test-2"}).encode()

        worker._on_message(ch, method, props, body)

        # basic_nack must have been called with requeue=False
        ch.basic_nack.assert_called_once_with(
            delivery_tag="tag-456", requeue=False
        )

    def test_nack_requeue_false_when_over_max_retries(self):
        """basic_nack must use requeue=False when retry count > max_retries."""
        worker = _make_failing_worker(max_retries=3)

        mock_redis = MagicMock()
        mock_redis.get.return_value = None
        mock_redis.set = MagicMock()
        mock_redis.setex = MagicMock()
        worker._redis_client = mock_redis
        worker._event_bus = MagicMock()

        ch = MagicMock()
        method = MagicMock()
        method.delivery_tag = "tag-789"

        props = self._make_props_with_xdeath(5)  # 5 > 3 → send to DLQ

        body = json.dumps({"job_id": "job-test-3"}).encode()

        worker._on_message(ch, method, props, body)

        ch.basic_nack.assert_called_once_with(
            delivery_tag="tag-789", requeue=False
        )

    def test_nack_requeue_true_on_first_failure(self):
        """On first failure (no x-death headers), basic_nack must use requeue=True."""
        worker = _make_failing_worker(max_retries=3)

        mock_redis = MagicMock()
        mock_redis.get.return_value = None
        mock_redis.set = MagicMock()
        mock_redis.setex = MagicMock()
        worker._redis_client = mock_redis
        worker._event_bus = MagicMock()

        ch = MagicMock()
        method = MagicMock()
        method.delivery_tag = "tag-first"

        props = self._make_props_with_xdeath(None)  # No x-death → first attempt

        body = json.dumps({"job_id": "job-test-first"}).encode()

        worker._on_message(ch, method, props, body)

        ch.basic_nack.assert_called_once_with(
            delivery_tag="tag-first", requeue=True
        )
