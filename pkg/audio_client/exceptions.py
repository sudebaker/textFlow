from pkg.shared.exceptions import ServiceUnavailableError


class WhisperServiceError(ServiceUnavailableError):
    """Raised when the Whisper transcription service fails after all retries."""

    def __init__(self, message: str):
        super().__init__(message, service_name="Whisper")
