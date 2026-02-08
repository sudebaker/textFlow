"""
Standardized logging module for Python workers.

Provides consistent logging across all workers, with structured output
compatible with Go's zerolog format for cross-service log correlation.

Usage:
    from pkg.logging_python import setup_logging, get_logger

    logger = setup_logging("embeddings-worker")
    logger.info("Processing job", extra={"job_id": "123"})
"""

import logging
import os
import json
from typing import Any, Dict, Optional
from datetime import datetime


class StructuredFormatter(logging.Formatter):
    """Custom formatter that outputs structured JSON logs."""

    def format(self, record: logging.LogRecord) -> str:
        """Format log record as structured JSON."""
        log_data = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        # Add extra fields from record.__dict__ (Python logging extra pattern)
        job_id = getattr(record, "job_id", None)
        worker = getattr(record, "worker", None)
        duration = getattr(record, "duration", None)
        ctx = getattr(record, "ctx", None)

        if job_id:
            log_data["job_id"] = job_id
        if worker:
            log_data["worker"] = worker
        if duration:
            log_data["duration_ms"] = duration
        if ctx:
            log_data["context"] = ctx

        # Add exception info if present
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_data)


def setup_logging(
    worker_name: str,
    level: Optional[str] = None,
    log_format: Optional[str] = None,
) -> logging.Logger:
    """
    Setup standardized logging for workers.

    Args:
        worker_name: Name of the worker (e.g., "embeddings-worker")
        level: Log level (DEBUG, INFO, WARNING, ERROR). Default from LOG_LEVEL env var.
        log_format: Format string. Use "json" for structured logging.

    Returns:
        Configured logger instance.

    Environment Variables:
        LOG_LEVEL: Log level (debug, info, warning, error)
        LOG_FORMAT: Format ("json" or "text"). Default: "json" in production.
    """
    # Get log level from environment or parameter
    log_level_str = level or os.getenv("LOG_LEVEL", "info")
    log_level = getattr(logging, log_level_str.upper(), logging.INFO)

    # Get format from environment
    log_format_type = log_format or os.getenv("LOG_FORMAT", "json")

    # Create logger
    logger = logging.getLogger(worker_name)
    logger.setLevel(log_level)

    # Avoid duplicate handlers
    if logger.handlers:
        return logger

    # Create handler
    handler = logging.StreamHandler()

    if log_format_type == "json":
        handler.setFormatter(StructuredFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        )

    logger.addHandler(handler)

    # Set specific logger levels for external libraries
    logging.getLogger("pika").setLevel(logging.WARNING)
    logging.getLogger("redis").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    return logger


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger with standardized setup.

    Args:
        name: Logger name (usually __name__)

    Returns:
        Logger instance
    """
    return logging.getLogger(name)


class JobLogger:
    """
    Context-aware logger for job processing.

    Automatically includes job_id in all log messages for correlation
    across services.

    Usage:
        logger = JobLogger("embeddings-worker")
        logger.start_job("123")
        logger.info("Processing text")
        logger.complete_job(2.5)
    """

    def __init__(self, worker_name: str, base_logger: Optional[logging.Logger] = None):
        """
        Initialize JobLogger.

        Args:
            worker_name: Name of the worker
            base_logger: Optional pre-configured logger
        """
        self.worker_name = worker_name
        self.logger = base_logger or setup_logging(worker_name)
        self.job_id: Optional[str] = None
        self.start_time: Optional[float] = None

    def start_job(self, job_id: str) -> "JobLogger":
        """Mark the start of a job with context."""
        self.job_id = job_id
        self.start_time = __import__("time").time()
        self.logger.info(
            f"Starting job",
            extra={"job_id": job_id, "worker": self.worker_name},
        )
        return self

    def log_processing(self, message: str, **kwargs) -> None:
        """Log a processing message with job context."""
        extra = {"job_id": self.job_id, "worker": self.worker_name}
        if kwargs:
            extra["ctx"] = kwargs
        self.logger.info(message, extra=extra)

    def log_warning(self, message: str, **kwargs) -> None:
        """Log a warning with job context."""
        extra = {"job_id": self.job_id, "worker": self.worker_name}
        if kwargs:
            extra["ctx"] = kwargs
        self.logger.warning(message, extra=extra)

    def log_error(
        self, message: str, error: Optional[Exception] = None, **kwargs
    ) -> None:
        """Log an error with job context."""
        extra = {"job_id": self.job_id, "worker": self.worker_name}
        if kwargs:
            extra["ctx"] = kwargs
        if error:
            extra["error"] = str(error)
        self.logger.error(message, extra=extra)

    def complete_job(self, duration: Optional[float] = None) -> float:
        """
        Mark job completion and log duration.

        Args:
            duration: Optional duration in seconds. Calculated if not provided.

        Returns:
            Duration in seconds
        """
        if duration is None and self.start_time:
            duration = __import__("time").time() - self.start_time

        self.logger.info(
            f"Job completed",
            extra={
                "job_id": self.job_id,
                "worker": self.worker_name,
                "duration_ms": round((duration or 0) * 1000, 2),
            },
        )

        self.job_id = None
        self.start_time = None

        return duration or 0

    def failed_job(self, error: str, duration: Optional[float] = None) -> None:
        """Mark job as failed with error context."""
        if duration is None and self.start_time:
            duration = __import__("time").time() - self.start_time

        self.logger.error(
            f"Job failed",
            extra={
                "job_id": self.job_id,
                "worker": self.worker_name,
                "error": error,
                "duration_ms": round((duration or 0) * 1000, 2),
            },
        )

        self.job_id = None
        self.start_time = None
