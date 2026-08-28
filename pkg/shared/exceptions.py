class ServiceUnavailableError(Exception):
    """Raised when an external service is unreachable after all retries."""

    def __init__(self, message: str, service_name: str | None = None):
        if service_name:
            message = f"{service_name}: {message}"
        super().__init__(message)
        self.service_name = service_name


class JobCancelledError(Exception):
    """Raised when a job has been cancelled and the worker should stop.

    Cooperative cancellation: workers check ``is_job_cancelled(job_id)`` at
    safe points and raise this error. The base class handler acks the
    message without requeue or failure marking, leaving the job in
    ``cancelled`` status.
    """
