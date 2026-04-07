from pkg.shared.exceptions import ServiceUnavailableError


class MultimodalLLMServiceError(ServiceUnavailableError):
    """Raised when the multimodal LLM service fails after all retries."""

    def __init__(self, message: str):
        super().__init__(message, service_name="MultimodalLLM")