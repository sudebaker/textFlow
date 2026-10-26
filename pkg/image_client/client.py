import mimetypes
import os
import threading
import time
from typing import Dict, List, Optional

import requests

from .exceptions import MultimodalLLMServiceError
from .models import ImageAnalysisResult

DEFAULT_BASE_URL = "http://multimodal-llm:8000"


def _urls_from_env(name: str) -> List[str]:
    """Read a comma-separated URL pool, falling back to the shared default."""
    raw = os.getenv(name, DEFAULT_BASE_URL)
    urls = [url.strip().rstrip("/") for url in raw.split(",") if url.strip()]
    return urls or [DEFAULT_BASE_URL]


class MultimodalLLMClientPool:
    """HTTP client pool for the image OCR and description services.

    OCR (`/analyze`) and description (`/describe`) are distinct capabilities
    with separate, env-configured endpoint pools (spec: image-ocr-description):

    - ``OCR_BASE_URL``: OCR service (comma-separated for failover)
    - ``IMAGE_BASE_URL``: description service (comma-separated for failover)

    Each pool round-robins independently. Requests retry up to
    ``MULTIMODAL_LLM_MAX_RETRIES`` with exponential backoff before failing.
    """

    def __init__(self):
        self._ocr_urls = _urls_from_env("OCR_BASE_URL")
        self._image_urls = _urls_from_env("IMAGE_BASE_URL")
        self._indices = {"ocr": 0, "image": 0}
        self._lock = threading.Lock()
        self._timeout = int(os.getenv("MULTIMODAL_LLM_TIMEOUT", "120"))
        self._max_retries = int(os.getenv("MULTIMODAL_LLM_MAX_RETRIES", "3"))
        self._verify_ssl = (
            os.getenv("MULTIMODAL_LLM_VERIFY_SSL", "true").lower() == "true"
        )

    def _next_url(self, pool: str, urls: List[str]) -> str:
        with self._lock:
            index = self._indices[pool]
            url = urls[index % len(urls)]
            self._indices[pool] = (index + 1) % len(urls)
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
            pool="ocr",
            urls=self._ocr_urls,
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
            pool="image",
            urls=self._image_urls,
            endpoint="/describe",
            image_bytes=image_bytes,
            filename=filename,
            data={"prompt": prompt} if prompt else None,
        )
        return result.get("description") or ""

    def _request(
        self,
        pool: str,
        urls: List[str],
        endpoint: str,
        image_bytes: bytes,
        filename: str,
        data: Optional[Dict[str, str]],
    ) -> Dict:
        """Call one capability pool with failover and retries."""
        for attempt in range(self._max_retries):
            url = self._next_url(pool, urls)
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
        """Send a single request to one capability endpoint."""
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
