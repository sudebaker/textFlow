"""
Tests for Redis reconnection logic in BaseWorker.

Verifies that:
- _get_redis() returns a working client on first call
- _get_redis() reconnects when ping() fails with ConnectionError
- _get_redis() reuses existing client when ping() succeeds
- _connect_redis() retries on connection failure with exponential backoff
"""

from unittest.mock import MagicMock, patch, call
import pytest
import redis as redis_lib


def _make_worker():
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
                                    worker_name="test-reconnect-worker",
                                    queue_name="test_queue",
                                    metrics_port=9995,
                                )
                                return worker


class TestGetRedis:
    """Tests for the _get_redis() lazy reconnection method."""

    def test_get_redis_returns_client_when_connection_is_healthy(self):
        """_get_redis() returns a working client on first call."""
        worker = _make_worker()

        mock_client = MagicMock()
        mock_client.ping.return_value = True

        with patch.object(worker, "_connect_redis", return_value=mock_client) as mock_connect:
            result = worker._get_redis()

        assert result is mock_client
        mock_connect.assert_called_once()

    def test_get_redis_reconnects_when_connection_is_lost(self):
        """_get_redis() creates a new client if ping() fails with ConnectionError."""
        worker = _make_worker()

        # Simulate stale connection
        stale_client = MagicMock()
        stale_client.ping.side_effect = redis_lib.ConnectionError("Connection lost")
        worker._redis_client = stale_client

        new_client = MagicMock()
        new_client.ping.return_value = True

        with patch.object(worker, "_connect_redis", return_value=new_client) as mock_connect:
            result = worker._get_redis()

        assert result is new_client
        assert result is not stale_client
        mock_connect.assert_called_once()

    def test_get_redis_reconnects_on_timeout_error(self):
        """_get_redis() creates a new client if ping() fails with TimeoutError."""
        worker = _make_worker()

        stale_client = MagicMock()
        stale_client.ping.side_effect = redis_lib.TimeoutError("Timeout")
        worker._redis_client = stale_client

        new_client = MagicMock()

        with patch.object(worker, "_connect_redis", return_value=new_client) as mock_connect:
            result = worker._get_redis()

        assert result is new_client
        mock_connect.assert_called_once()

    def test_get_redis_returns_existing_client_when_healthy(self):
        """_get_redis() reuses existing client if ping() succeeds."""
        worker = _make_worker()

        existing_client = MagicMock()
        existing_client.ping.return_value = True
        worker._redis_client = existing_client

        with patch.object(worker, "_connect_redis") as mock_connect:
            result = worker._get_redis()

        assert result is existing_client
        mock_connect.assert_not_called()

    def test_get_redis_initializes_client_when_none(self):
        """_get_redis() creates client when _redis_client is None."""
        worker = _make_worker()
        assert worker._redis_client is None

        mock_client = MagicMock()

        with patch.object(worker, "_connect_redis", return_value=mock_client):
            result = worker._get_redis()

        assert result is mock_client
        assert worker._redis_client is mock_client


class TestConnectRedis:
    """Tests for the _connect_redis() method with retry logic."""

    def test_connect_redis_returns_client_on_success(self):
        """_connect_redis() returns a connected Redis client on first attempt."""
        worker = _make_worker()

        mock_client = MagicMock()
        mock_client.ping.return_value = True

        with patch("redis.from_url", return_value=mock_client) as mock_from_url:
            result = worker._connect_redis()

        assert result is mock_client
        mock_from_url.assert_called_once_with(worker.redis_url, decode_responses=True)
        mock_client.ping.assert_called_once()

    def test_connect_redis_retries_on_connection_error(self):
        """_connect_redis() retries when ConnectionError is raised."""
        worker = _make_worker()

        good_client = MagicMock()
        good_client.ping.return_value = True

        call_count = 0
        clients = []

        def mock_from_url(url, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                client = MagicMock()
                client.ping.side_effect = redis_lib.ConnectionError("Refused")
                clients.append(client)
                return client
            clients.append(good_client)
            return good_client

        with patch("redis.from_url", side_effect=mock_from_url):
            with patch("time.sleep") as mock_sleep:
                result = worker._connect_redis()

        assert result is good_client
        assert call_count == 3
        assert mock_sleep.call_count == 2  # slept twice before success on 3rd

    def test_connect_redis_raises_after_max_attempts(self):
        """_connect_redis() raises ConnectionError after 5 failed attempts."""
        worker = _make_worker()

        failing_client = MagicMock()
        failing_client.ping.side_effect = redis_lib.ConnectionError("Refused")

        with patch("redis.from_url", return_value=failing_client):
            with patch("time.sleep"):
                with pytest.raises(redis_lib.ConnectionError):
                    worker._connect_redis()

    def test_connect_redis_max_5_attempts(self):
        """_connect_redis() tries exactly 5 times before giving up."""
        worker = _make_worker()

        attempt_count = 0

        def mock_from_url(url, **kwargs):
            nonlocal attempt_count
            attempt_count += 1
            client = MagicMock()
            client.ping.side_effect = redis_lib.ConnectionError("Refused")
            return client

        with patch("redis.from_url", side_effect=mock_from_url):
            with patch("time.sleep"):
                with pytest.raises(redis_lib.ConnectionError):
                    worker._connect_redis()

        assert attempt_count == 5
