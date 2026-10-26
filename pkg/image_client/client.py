import mimetypes
import os
import threading
import time
from typing import Dict, Optional

import requests

from .exceptions import MultimodalLLMServiceError
from .models import ImageAnalysisResult


class MultimodalLLMClientPool:
    """HTTP client pool for the multimodal image analysis service.

    Reads MULTIMODAL_LLM_URLS env var (comma-separated). The service exposes
    two distinct capabilities, both on this pool:

    - ``POST /analyze``: OCR (visible text)
    - ``POST /describe``: semantic description (``full`` profile only)

    Thread-safe round-robin selection with automatic failover to next URL on
    error. Retries up to MULTIMODAL_LLM_MAX_RETRIES with exponential backoff
    before failing.
    """

    def __init__(self):
        urls_env = os.getenv("MULTIMODAL_LLM_URLS", "http://multimodal-llm:8000")
        self._urls = [u.strip().rstrip("/") for u in urls_env.split(",") if u.strip()]
        self._index = 0
        self._lock = threading.Lock()
        self._timeout = int(os.getenv("MULTIMODAL_LLM_TIMEOUT", "120"))
        self._max_retries = int(os.getenv("MULTIMODAL_LLM_MAX_RETRIES", "3"))
        self._verify_ssl = (
            os.getenv("MULTIMODAL_LLM_VERIFY_SSL", "true").lower() == "true"
        )

    def _next_url(self) -> str:
        with self._lock:
            url = self._urls[self._index % len(self._urls)]
            self._index = (self._index + 1) % len(self._urls)
        return url

    def analyze(
        self,
        image_bytes: bytes,
        filename: str,
        prompt: Optional[str] = None,
    ) -> ImageAnalysisResult:
        """Extract visible text from an image via the OCR endpoint.

        Args:
            image_bytes: Raw image file bytes
            filename: Original filename
            prompt: Optional custom OCR prompt

        Returns:
            ImageAnalysisResult with extracted_text, description, language, confidence

        Raises:
            MultimodalLLMServiceError: If all URLs fail after max retries
        """
        result = self._request(
            endpoint="/analyze",
            image_bytes=image_bytes,
            filename=filename,
            data={"prompt": prompt} if prompt else None,
        )

        return ImageAnalysisResult(
            extracted_text=result.get("extracted_text", ""),
            description=result.get("description"),
            language=result.get("language"),
            confidence=result.get("confidence"),
        )

    def describe(
        self,
        image_bytes: bytes,
        filename: str,
        prompt: Optional[str] = None,
    ) -> str:
        """Return a semantic description of the image (full profile only).

        Args:
            image_bytes: Raw image file bytes
            filename: Original filename
            prompt: Optional custom description prompt

        Returns:
            Description text, or "" when the service returns none.

        Raises:
            MultimodalLLMServiceError: If all URLs fail after max retries
        """
        result = self._request(
            endpoint="/describe",
            image_bytes=image_bytes,
            filename=filename,
            data={"prompt": prompt} if prompt else None,
        )
        return result.get("description") or ""

    def _request(
        self,
        endpoint: str,
        image_bytes: bytes,
        filename: str,
        data: Optional[Dict[str, str]],
    ) -> Dict:
        """Call the service endpoint with failover and retries."""
        for attempt in range(self._max_retries):
            url = self._next_url()
            try:
                return self._request_single(
                    url, endpoint, image_bytes, filename, data or {}
                )
            except requests.exceptions.RequestException as e:
                backoff = min(2**attempt, 30)
                if attempt < self._max_retries - 1:
                    time.sleep(backoff)
                    continue
                raise MultimodalLLMServiceError(
                    f"Multimodal LLM service unavailable after {self._max_retries} attempts: {e}"
                )
            except Exception as e:
                if "service unavailable" in str(e).lower():
                    backoff = min(2**attempt, 30)
                    if attempt < self._max_retries - 1:
                        time.sleep(backoff)
                        continue
                raise MultimodalLLMServiceError(f"Image analysis failed: {e}")

        raise MultimodalLLMServiceError(f"Failed after {self._max_retries} attempts")

    def _request_single(
        self,
        url: str,
        endpoint: str,
        image_bytes: bytes,
        filename: str,
        data: Dict[str, str],
    ) -> Dict:
        """Send a single request to one service URL."""
        request_url = f"{url}{endpoint}"

        # Infer MIME type from filename extension
        mime_type, _ = mimetypes.guess_type(filename)
        if mime_type is None:
            mime_type = "application/octet-stream"

        files = {"file": (filename, image_bytes, mime_type)}

        response = requests.post(
            request_url,
            files=files,
            data=data,
            timeout=self._timeout,
            verify=self._verify_ssl,
        )

        if response.status_code >= 500:
            raise requests.exceptions.RequestException(
                f"Server error: {response.status_code}"
            )

        if response.status_code != 200:
            error_body = response.text[:500] if response.text else "no response body"
            raise Exception(
                f"Unexpected status code: {response.status_code} - {error_body}"
            )

        return response.json()
