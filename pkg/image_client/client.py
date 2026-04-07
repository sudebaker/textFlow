import os
import threading
import time
from typing import Optional

import requests

from .exceptions import MultimodalLLMServiceError
from .models import ImageAnalysisResult


class MultimodalLLMClientPool:
    """HTTP client pool for multimodal LLM image analysis service.
    
    Reads MULTIMODAL_LLM_URLS env var (comma-separated).
    Thread-safe round-robin selection with automatic failover to next URL on error.
    Retries up to MAX_RETRIES=3 with exponential backoff before failing.
    """

    def __init__(self):
        urls_env = os.getenv("MULTIMODAL_LLM_URLS", "http://multimodal-llm:8000")
        self._urls = [u.strip() for u in urls_env.split(",") if u.strip()]
        self._index = 0
        self._lock = threading.Lock()
        self._timeout = int(os.getenv("MULTIMODAL_LLM_TIMEOUT", "120"))
        self._max_retries = int(os.getenv("MULTIMODAL_LLM_MAX_RETRIES", "3"))
        self._verify_ssl = os.getenv("MULTIMODAL_LLM_VERIFY_SSL", "true").lower() == "true"

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
        """Analyze image via multimodal LLM service.
        
        Args:
            image_bytes: Raw image file bytes
            filename: Original filename
            prompt: Optional custom prompt
            
        Returns:
            ImageAnalysisResult with extracted_text, description, language, confidence
            
        Raises:
            MultimodalLLMServiceError: If all URLs fail after max retries
        """
        for attempt in range(self._max_retries):
            url = self._next_url()
            try:
                return self._analyze_single(url, image_bytes, filename, prompt)
            except requests.exceptions.RequestException as e:
                backoff = min(2 ** attempt, 30)
                if attempt < self._max_retries - 1:
                    time.sleep(backoff)
                    continue
                raise MultimodalLLMServiceError(
                    f"Multimodal LLM service unavailable after {self._max_retries} attempts: {e}"
                )
            except Exception as e:
                if "service unavailable" in str(e).lower():
                    backoff = min(2 ** attempt, 30)
                    if attempt < self._max_retries - 1:
                        time.sleep(backoff)
                        continue
                raise MultimodalLLMServiceError(f"Image analysis failed: {e}")

        raise MultimodalLLMServiceError(f"Failed after {self._max_retries} attempts")

    def _analyze_single(
        self,
        url: str,
        image_bytes: bytes,
        filename: str,
        prompt: Optional[str],
    ) -> ImageAnalysisResult:
        """Send a single image analysis request to one URL."""
        endpoint = f"{url}/analyze"
        
        files = {"file": (filename, image_bytes)}
        data = {}
        if prompt:
            data["prompt"] = prompt
        
        response = requests.post(
            endpoint,
            files=files,
            data=data,
            timeout=self._timeout,
            verify=self._verify_ssl,
        )
        
        if response.status_code >= 500:
            raise requests.exceptions.RequestException(f"Server error: {response.status_code}")
        
        if response.status_code != 200:
            raise Exception(f"Unexpected status code: {response.status_code}")
        
        result = response.json()
        
        return ImageAnalysisResult(
            extracted_text=result.get("extracted_text", ""),
            description=result.get("description"),
            language=result.get("language"),
            confidence=result.get("confidence"),
        )