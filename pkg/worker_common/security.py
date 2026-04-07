import asyncio
import logging
import os
import signal
from typing import Callable, Optional

logger = logging.getLogger(__name__)


def validate_upload_path(file_path: str, allowed_dir: str) -> str:
    """Validate that file path is within allowed directory to prevent path traversal."""
    abs_allowed = os.path.abspath(allowed_dir)
    abs_file = os.path.abspath(file_path)

    if not abs_file.startswith(abs_allowed + os.sep) and abs_file != abs_allowed:
        raise ValueError(f"Invalid file path: {file_path} is not within {allowed_dir}")

    return abs_file


def create_signal_handler(connection):
    """Create asyncio-compatible signal handler for aio_pika connections."""
    loop = asyncio.get_event_loop()

    def signal_handler(sig, frame):
        sig_name = "SIGTERM" if sig == signal.SIGTERM else "SIGINT"
        logger.info(f"Received {sig_name}, shutting down...")
        loop.call_soon_threadsafe(connection.close_loop)

    return signal_handler


def register_signal_handlers(connection):
    """Register signal handlers for graceful shutdown of aio_pika workers."""
    handler = create_signal_handler(connection)
    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)
    logger.info("Signal handlers registered for graceful shutdown")