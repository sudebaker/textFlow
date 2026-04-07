class ServiceUnavailableError(Exception):
    """Raised when an external service is unreachable after all retries."""

    def __init__(self, message: str, service_name: str | None = None):
        if service_name:
            message = f"{service_name}: {message}"
        super().__init__(message)
        self.service_name = service_name
