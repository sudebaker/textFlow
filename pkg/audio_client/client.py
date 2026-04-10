import os
import threading
import time
from typing import Optional

import requests

from .exceptions import WhisperServiceError
from .models import AudioSegment, TranscriptionResult


class WhisperClientPool:
    """HTTP client pool for Whisper transcription service.
    
    Reads WHISPER_URLS env var (comma-separated).
    Thread-safe round-robin selection with automatic failover to next URL on error.
    Retries up to MAX_RETRIES=3 with exponential backoff before failing.
    """

    def __init__(self):
        urls_env = os.getenv("WHISPER_URLS", "http://whisper:8080")
        self._urls = [u.strip() for u in urls_env.split(",") if u.strip()]
        self._index = 0
        self._lock = threading.Lock()
        self._timeout = int(os.getenv("WHISPER_TIMEOUT", "300"))
        self._max_retries = int(os.getenv("WHISPER_MAX_RETRIES", "3"))
        self._verify_ssl = os.getenv("WHISPER_VERIFY_SSL", "true").lower() == "true"

    def _next_url(self) -> str:
        with self._lock:
            url = self._urls[self._index % len(self._urls)]
            self._index = (self._index + 1) % len(self._urls)
        return url

    def transcribe(
        self,
        audio_bytes: bytes,
        filename: str,
        language: Optional[str] = None,
        diarize: bool = False,
    ) -> TranscriptionResult:
        """Transcribe audio file via Whisper service.
        
        Args:
            audio_bytes: Raw audio file bytes
            filename: Original filename for content-type detection
            language: Optional language code (e.g., "es", "en")
            diarize: Enable speaker diarization
            
        Returns:
            TranscriptionResult with text, language, duration, and optional segments
            
        Raises:
            WhisperServiceError: If all URLs fail after max retries
        """
        for attempt in range(self._max_retries):
            url = self._next_url()
            try:
                return self._transcribe_custom(url, audio_bytes, filename, language, diarize)
            except requests.exceptions.RequestException as e:
                backoff = min(2 ** attempt, 30)
                if attempt < self._max_retries - 1:
                    time.sleep(backoff)
                    continue
                raise WhisperServiceError(
                    f"Whisper service unavailable after {self._max_retries} attempts: {e}"
                )
            except Exception as e:
                if "service unavailable" in str(e).lower():
                    backoff = min(2 ** attempt, 30)
                    if attempt < self._max_retries - 1:
                        time.sleep(backoff)
                        continue
                raise WhisperServiceError(f"Transcription failed: {e}")

        raise WhisperServiceError(f"Failed after {self._max_retries} attempts")

    def _transcribe_single(
        self,
        url: str,
        audio_bytes: bytes,
        filename: str,
        language: Optional[str],
        diarize: bool,
    ) -> TranscriptionResult:
        """Send a single transcription request to one URL."""
        return self._transcribe_custom(url, audio_bytes, filename, language, diarize)
    
    def _transcribe_custom(
        self,
        url: str,
        audio_bytes: bytes,
        filename: str,
        language: Optional[str],
        diarize: bool,
    ) -> TranscriptionResult:
        """Send request to custom /transcribe endpoint."""
        endpoint = f"{url}/transcribe"
        
        files = {"audio": (filename, audio_bytes)}
        data = {}
        if language:
            data["language"] = language
        if diarize:
            data["diarize"] = "true"
        
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
        
        segments = None
        if result.get("segments"):
            segments = [
                AudioSegment(
                    start=seg["start"],
                    end=seg["end"],
                    text=seg["text"],
                    speaker=seg.get("speaker"),
                )
                for seg in result["segments"]
            ]
        
        return TranscriptionResult(
            text=result.get("text", ""),
            language=result.get("language"),
            duration_seconds=result.get("duration"),
            segments=segments,
        )
    

