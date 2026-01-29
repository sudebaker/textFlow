"""
Signal handling utilities for workers.
"""

import logging
import signal
import sys
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class SignalHandler:
    """
    Handles graceful shutdown signals for workers.

    Example:
        >>> handler = SignalHandler()
        >>> handler.register()
        >>>
        >>> while not handler.should_exit:
        >>>     # Process messages
        >>>     pass
    """

    def __init__(self):
        self.should_exit = False
        self._cleanup_callback: Optional[Callable] = None

    def register(self, cleanup_callback: Optional[Callable] = None):
        """
        Register signal handlers for SIGTERM and SIGINT.

        Args:
            cleanup_callback: Optional function to call before exit
        """
        self._cleanup_callback = cleanup_callback
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        logger.info("Signal handlers registered")

    def _handle_signal(self, signum, frame):
        """Handle shutdown signal."""
        signal_name = "SIGTERM" if signum == signal.SIGTERM else "SIGINT"
        logger.info(f"Received {signal_name}, initiating graceful shutdown...")
        self.should_exit = True

        if self._cleanup_callback:
            try:
                logger.info("Running cleanup callback...")
                self._cleanup_callback()
            except Exception as e:
                logger.error(f"Error during cleanup: {e}")

        logger.info("Shutdown complete")
        sys.exit(0)
