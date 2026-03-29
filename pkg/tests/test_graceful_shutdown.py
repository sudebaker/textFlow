"""
Tests for graceful shutdown behavior in BaseWorker.

RED phase: These tests verify the _stopping flag pattern for graceful shutdown.
The signal handler must:
  1. Set _stopping = True
  2. NOT call sys.exit()
"""

import signal
import sys
from unittest.mock import MagicMock, patch


def _make_worker():
    """Create a minimal BaseWorker instance without real connections."""
    mock_registry = MagicMock()

    with patch("pkg.worker_common.base.setup_logging", return_value=MagicMock()):
        with patch("pkg.worker_common.base.JobLogger", return_value=MagicMock()):
            with patch("pkg.worker_common.base.FastAPI", return_value=MagicMock()):
                with patch("pkg.worker_common.base.EventBus", return_value=MagicMock()):
                    # Patch Prometheus metric classes to avoid duplicate-registration errors
                    with patch("pkg.worker_common.base.Counter", return_value=MagicMock()):
                        with patch("pkg.worker_common.base.Histogram", return_value=MagicMock()):
                            with patch("pkg.worker_common.base.Gauge", return_value=MagicMock()):
                                from pkg.worker_common.base import BaseWorker

                                # Subclass to avoid NotImplementedError on process_message
                                class TestWorker(BaseWorker):
                                    def process_message(self, message):
                                        return None

                                worker = TestWorker(
                                    worker_name="test-worker",
                                    queue_name="test_queue",
                                    metrics_port=9999,
                                )
                                return worker


class TestGracefulShutdownFlag:
    """Tests that verify the _stopping flag is set on signal reception."""

    def test_signal_handler_sets_stopping_flag(self):
        """After receiving SIGTERM, _stopping must be True."""
        worker = _make_worker()

        # Verify initial state
        assert worker._stopping is False, "_stopping should start as False"

        # Simulate receiving SIGTERM
        worker._signal_handler(signal.SIGTERM, None)

        assert worker._stopping is True, "_stopping must be True after signal"

    def test_signal_handler_sets_stopping_flag_on_sigint(self):
        """After receiving SIGINT, _stopping must also be True."""
        worker = _make_worker()

        worker._signal_handler(signal.SIGINT, None)

        assert worker._stopping is True

    def test_signal_handler_does_not_call_sys_exit(self):
        """Signal handler must NOT call sys.exit() — that interrupts ongoing processing."""
        worker = _make_worker()

        with patch("sys.exit") as mock_exit:
            worker._signal_handler(signal.SIGTERM, None)
            mock_exit.assert_not_called()

    def test_signal_handler_does_not_call_sys_exit_on_sigint(self):
        """sys.exit must not be called on SIGINT either."""
        worker = _make_worker()

        with patch("sys.exit") as mock_exit:
            worker._signal_handler(signal.SIGINT, None)
            mock_exit.assert_not_called()


class TestOnMessageProcessed:
    """Tests for _on_message_processed helper."""

    def test_on_message_processed_stops_consumer_when_stopping(self):
        """When _stopping is True, _on_message_processed must call stop_consuming."""
        worker = _make_worker()
        worker._stopping = True

        mock_channel = MagicMock()
        mock_channel.is_open = True
        worker._channel = mock_channel

        worker._on_message_processed()

        mock_channel.stop_consuming.assert_called_once()

    def test_on_message_processed_does_not_stop_consumer_when_not_stopping(self):
        """When _stopping is False, _on_message_processed must not stop consuming."""
        worker = _make_worker()
        worker._stopping = False

        mock_channel = MagicMock()
        mock_channel.is_open = True
        worker._channel = mock_channel

        worker._on_message_processed()

        mock_channel.stop_consuming.assert_not_called()

    def test_on_message_processed_handles_missing_channel(self):
        """_on_message_processed must not crash if _channel is not set."""
        worker = _make_worker()
        worker._stopping = True

        # No _channel attribute set — should not raise
        if hasattr(worker, "_channel"):
            del worker._channel

        # Must not raise AttributeError
        worker._on_message_processed()
