"""
Tests for the DLQ (Dead Letter Queue) pattern implementation in BaseWorker.

Verifies that:
- x-death header retry counting is correct
- Messages are retried up to max_retries, then sent to DLQ
- basic_nack is called with requeue=False when max retries exceeded
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
    """Tests that transient errors use _should_retry for the correct ack/nack behaviour.

    With ``DELAYED_EXCHANGE_ENABLED=true`` (default):
      - Under max retries → basic_publish to delayed exchange + basic_ack
      - At/over max retries → basic_nack(requeue=False)

    With ``DELAYED_EXCHANGE_ENABLED=false`` (legacy fallback):
      - Under max retries → time.sleep + basic_nack(requeue=True)
      - At/over max retries → basic_nack(requeue=False)
    """

    def _make_props_with_xdeath(self, count):
        """Helper: create mock properties with x-death count."""
        props = MagicMock()
        if count is None:
            props.headers = None
        else:
            props.headers = {"x-death": [{"count": count}]}
        return props

    # ------------------------------------------------------------------
    # Delayed-exchange path (DELAYED_EXCHANGE_ENABLED=true, default)
    # ------------------------------------------------------------------

    def test_basic_ack_and_publish_when_under_max_retries(self, monkeypatch):
        """With delayed exchange enabled, retry → basic_publish + basic_ack (no nack)."""
        monkeypatch.setenv("DELAYED_EXCHANGE_ENABLED", "true")
        worker = _make_failing_worker(max_retries=3)

        mock_redis = MagicMock()
        worker._redis_client = mock_redis
        worker._event_bus = MagicMock()

        ch = MagicMock()
        method = MagicMock()
        method.delivery_tag = "tag-123"
        method.routing_key = "test_queue"

        props = self._make_props_with_xdeath(1)  # 1 < 3 → should retry
        props.content_type = "application/json"

        body = json.dumps({"job_id": "job-test-1"}).encode()

        worker._on_message(ch, method, props, body)

        # Must have published to the delayed exchange
        ch.basic_publish.assert_called_once()
        call_kwargs = ch.basic_publish.call_args
        assert call_kwargs.kwargs["exchange"] == "document_processor_delayed"
        assert call_kwargs.kwargs["routing_key"] == "test_queue"

        # Must have ACKed the original message
        ch.basic_ack.assert_called_once_with(delivery_tag="tag-123")
        ch.basic_nack.assert_not_called()

    def test_retry_headers_contain_x_retry_count(self, monkeypatch):
        """Re-published message must have x-retry-count set to next_attempt."""
        monkeypatch.setenv("DELAYED_EXCHANGE_ENABLED", "true")
        worker = _make_failing_worker(max_retries=3)
        worker._redis_client = MagicMock()
        worker._event_bus = MagicMock()

        ch = MagicMock()
        method = MagicMock()
        method.delivery_tag = "tag-hdr"
        method.routing_key = "test_queue"

        props = self._make_props_with_xdeath(1)  # retry_count=1 → next_attempt=2
        props.content_type = "application/json"

        body = json.dumps({"job_id": "job-hdr"}).encode()
        worker._on_message(ch, method, props, body)

        published_props = ch.basic_publish.call_args.kwargs["properties"]
        assert published_props.headers["x-retry-count"] == 2

    def test_basic_ack_and_publish_on_first_failure(self, monkeypatch):
        """On first failure (no prior retry headers), retry via delayed exchange."""
        monkeypatch.setenv("DELAYED_EXCHANGE_ENABLED", "true")
        worker = _make_failing_worker(max_retries=3)
        worker._redis_client = MagicMock()
        worker._event_bus = MagicMock()

        ch = MagicMock()
        method = MagicMock()
        method.delivery_tag = "tag-first"
        method.routing_key = "test_queue"

        props = self._make_props_with_xdeath(None)  # No prior retries
        props.content_type = "application/json"

        body = json.dumps({"job_id": "job-first"}).encode()
        worker._on_message(ch, method, props, body)

        ch.basic_publish.assert_called_once()
        ch.basic_ack.assert_called_once_with(delivery_tag="tag-first")
        ch.basic_nack.assert_not_called()

    # ------------------------------------------------------------------
    # DLQ path (max retries exceeded) — same regardless of exchange mode
    # ------------------------------------------------------------------

    def test_nack_requeue_false_when_max_retries_exceeded(self):
        """basic_nack must be called with requeue=False when retry count >= max_retries."""
        worker = _make_failing_worker(max_retries=3)

        mock_redis = MagicMock()
        worker._redis_client = mock_redis
        worker._event_bus = MagicMock()

        ch = MagicMock()
        method = MagicMock()
        method.delivery_tag = "tag-456"

        props = self._make_props_with_xdeath(3)  # 3 == 3 → send to DLQ

        body = json.dumps({"job_id": "job-test-2"}).encode()

        worker._on_message(ch, method, props, body)

        ch.basic_nack.assert_called_once_with(
            delivery_tag="tag-456", requeue=False
        )

    def test_nack_requeue_false_when_over_max_retries(self):
        """basic_nack must use requeue=False when retry count > max_retries."""
        worker = _make_failing_worker(max_retries=3)

        mock_redis = MagicMock()
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

    # ------------------------------------------------------------------
    # Legacy fallback path (DELAYED_EXCHANGE_ENABLED=false)
    # ------------------------------------------------------------------

    @patch('pkg.worker_common.base.time.sleep', return_value=None)
    def test_nack_requeue_true_when_delayed_exchange_disabled(self, mock_sleep, monkeypatch):
        """With DELAYED_EXCHANGE_ENABLED=false, retry falls back to nack(requeue=True)."""
        monkeypatch.setenv("DELAYED_EXCHANGE_ENABLED", "false")
        worker = _make_failing_worker(max_retries=3)
        worker._redis_client = MagicMock()
        worker._event_bus = MagicMock()

        ch = MagicMock()
        method = MagicMock()
        method.delivery_tag = "tag-legacy"
        method.routing_key = "test_queue"

        props = self._make_props_with_xdeath(1)  # 1 < 3 → should retry

        body = json.dumps({"job_id": "job-legacy"}).encode()
        worker._on_message(ch, method, props, body)

        # Legacy path: sleep then nack with requeue=True
        mock_sleep.assert_called_once()
        ch.basic_nack.assert_called_once_with(delivery_tag="tag-legacy", requeue=True)
        ch.basic_publish.assert_not_called()

    @patch('pkg.worker_common.base.time.sleep', return_value=None)
    def test_nack_requeue_true_on_first_failure_legacy(self, mock_sleep, monkeypatch):
        """With DELAYED_EXCHANGE_ENABLED=false and no prior headers, nack(requeue=True)."""
        monkeypatch.setenv("DELAYED_EXCHANGE_ENABLED", "false")
        worker = _make_failing_worker(max_retries=3)
        worker._redis_client = MagicMock()
        worker._event_bus = MagicMock()

        ch = MagicMock()
        method = MagicMock()
        method.delivery_tag = "tag-first-legacy"
        method.routing_key = "test_queue"

        props = self._make_props_with_xdeath(None)

        body = json.dumps({"job_id": "job-first-legacy"}).encode()
        worker._on_message(ch, method, props, body)

        ch.basic_nack.assert_called_once_with(delivery_tag="tag-first-legacy", requeue=True)

    # ------------------------------------------------------------------
    # Fallback when delayed exchange publish fails
    # ------------------------------------------------------------------

    @patch('pkg.worker_common.base.time.sleep', return_value=None)
    def test_fallback_to_nack_when_publish_fails(self, mock_sleep, monkeypatch):
        """If publishing to delayed exchange raises, fall back to nack(requeue=True)."""
        monkeypatch.setenv("DELAYED_EXCHANGE_ENABLED", "true")
        worker = _make_failing_worker(max_retries=3)
        worker._redis_client = MagicMock()
        worker._event_bus = MagicMock()

        ch = MagicMock()
        ch.basic_publish.side_effect = Exception("connection lost")
        method = MagicMock()
        method.delivery_tag = "tag-fallback"
        method.routing_key = "test_queue"

        props = self._make_props_with_xdeath(1)
        props.content_type = "application/json"

        body = json.dumps({"job_id": "job-fallback"}).encode()
        worker._on_message(ch, method, props, body)

        # basic_publish was attempted, failed, then fell back to nack+requeue
        ch.basic_publish.assert_called_once()
        mock_sleep.assert_called_once()
        ch.basic_nack.assert_called_once_with(delivery_tag="tag-fallback", requeue=True)
