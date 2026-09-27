"""Shared RabbitMQ queue declaration arguments.

Single source of truth for the queue ``arguments`` table so every Python
declaration site passes byte-identical arguments, matching the Go
orchestrator (internal/broker/rabbitmq.go:declareQueue). Diverging
arguments cause RabbitMQ PRECONDITION_FAILED ("inequivalent arg")
reconnection loops — see AGENTS.md "RabbitMQ Queue Declaration (CRITICAL)".
"""

from typing import Any, Dict

from pkg.worker_common.config import get_int_env

# Dead Letter Exchange config — must match internal/broker/rabbitmq.go
DLX_EXCHANGE = "document_processor_dlx"

# Max messages per queue before publishers are rejected (0 = unlimited).
# Must stay in sync with QUEUE_MAX_LENGTH in internal/config/config.go.
QUEUE_MAX_LENGTH_ENV = "QUEUE_MAX_LENGTH"
DEFAULT_QUEUE_MAX_LENGTH = 1000


def get_queue_max_length() -> int:
    """Return the configured queue length limit (0 = unlimited)."""
    return get_int_env(QUEUE_MAX_LENGTH_ENV, DEFAULT_QUEUE_MAX_LENGTH)


def build_queue_arguments(queue_name: str) -> Dict[str, Any]:
    """Build the queue arguments table shared by all declaration sites.

    Mirrors internal/broker/rabbitmq.go:declareQueue(): the DLX pair is
    always present; x-max-length / x-overflow are only added when
    QUEUE_MAX_LENGTH > 0 so both languages declare identical arguments.

    Args:
        queue_name: Name of the queue to be declared.

    Returns:
        Arguments table for queue_declare / declare_queue.
    """
    arguments: Dict[str, Any] = {
        "x-dead-letter-exchange": DLX_EXCHANGE,
        "x-dead-letter-routing-key": f"{queue_name}_failed",
    }
    max_length = get_queue_max_length()
    if max_length > 0:
        arguments["x-max-length"] = max_length
        arguments["x-overflow"] = "reject-publish"
    return arguments
