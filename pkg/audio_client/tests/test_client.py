import json
import threading
from unittest.mock import patch, MagicMock

import pytest
import requests

from pkg.audio_client.client import WhisperClientPool
from pkg.audio_client.exceptions import WhisperServiceError
from pkg.audio_client.models import AudioSegment, TranscriptionResult


class TestWhisperClientPool:
    """Tests for WhisperClientPool."""

    def test_default_url(self):
        """Test default URL is used when env var not set."""
        with patch.dict("os.environ", {}, clear=False):
            client = WhisperClientPool()
            assert "http://whisper:8080" in client._urls

    def test_custom_urls(self):
        """Test custom URLs from env var."""
        with patch.dict(
            "os.environ",
            {"WHISPER_URLS": "http://whisper-1:9000,http://whisper-2:9000"},
            clear=False,
        ):
            client = WhisperClientPool()
            assert len(client._urls) == 2
            assert client._urls[0] == "http://whisper-1:9000"
            assert client._urls[1] == "http://whisper-2:9000"

    def test_round_robin_selection(self):
        """Test round-robin URL selection advances index."""
        with patch.dict(
            "os.environ",
            {"WHISPER_URLS": "http://whisper-1:9000,http://whisper-2:9000"},
            clear=False,
        ):
            client = WhisperClientPool()
            urls = []
            for _ in range(4):
                urls.append(client._next_url())
            assert urls[0] == "http://whisper-1:9000"
            assert urls[1] == "http://whisper-2:9000"
            assert urls[2] == "http://whisper-1:9000"
            assert urls[3] == "http://whisper-2:9000"

    def test_thread_safe_index(self):
        """Test thread-safe index access."""
        with patch.dict(
            "os.environ",
            {"WHISPER_URLS": "http://whisper-1:9000,http://whisper-2:9000"},
            clear=False,
        ):
            client = WhisperClientPool()
            errors = []

            def access_url():
                try:
                    for _ in range(100):
                        client._next_url()
                except Exception as e:
                    errors.append(e)

            threads = [threading.Thread(target=access_url) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert len(errors) == 0

    def test_transcribe_success(self, mocker):
        """Test successful transcription response parsing."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "text": "Hola mundo",
            "language": "es",
            "duration": 5.5,
            "segments": [
                {"start": 0.0, "end": 2.0, "text": "Hola"},
                {"start": 2.1, "end": 5.5, "text": "mundo"},
            ],
        }

        mocker.patch("requests.post", return_value=mock_response)

        client = WhisperClientPool()
        result = client.transcribe(
            audio_bytes=b"fake audio",
            filename="test.mp3",
            language="es",
            diarize=True,
        )

        assert isinstance(result, TranscriptionResult)
        assert result.text == "Hola mundo"
        assert result.language == "es"
        assert result.duration_seconds == 5.5
        assert len(result.segments) == 2

    def test_transcribe_failover(self, mocker):
        """Test failover: first URL returns 500, second URL succeeds."""
        mock_500 = MagicMock()
        mock_500.status_code = 500

        mock_success = MagicMock()
        mock_success.status_code = 200
        mock_success.json.return_value = {
            "text": "Transcription OK",
            "language": "en",
            "duration": 5.0,
        }

        mocker.patch(
            "requests.post",
            side_effect=[mock_500, mock_success],
        )

        with patch.dict(
            "os.environ",
            {
                "WHISPER_URLS": "http://whisper-1:8080,http://whisper-2:8080",
                "WHISPER_MAX_RETRIES": "2",
            },
            clear=False,
        ):
            client = WhisperClientPool()
            result = client.transcribe(
                audio_bytes=b"audio",
                filename="test.mp3",
            )

            assert result.text == "Transcription OK"

    def test_transcribe_max_retries_exceeded(self, mocker):
        """Test max retries exceeded raises ServiceUnavailableError."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mocker.patch("requests.post", return_value=mock_response)

        with patch.dict(
            "os.environ",
            {"WHISPER_URLS": "http://whisper-1:8080", "WHISPER_MAX_RETRIES": "1"},
            clear=False,
        ):
            client = WhisperClientPool()
            with pytest.raises(WhisperServiceError):
                client.transcribe(
                    audio_bytes=b"audio",
                    filename="test.mp3",
                )

    def test_transcribe_no_segments(self, mocker):
        """Test fallback to simple chunking when no segments."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "text": "Simple transcription",
            "language": "en",
            "duration": 10.0,
        }

        mocker.patch("requests.post", return_value=mock_response)

        client = WhisperClientPool()
        result = client.transcribe(
            audio_bytes=b"audio",
            filename="test.mp3",
            diarize=False,
        )

        assert result.text == "Simple transcription"
        assert result.segments is None

    def test_timeout_config(self):
        """Test timeout is read from env."""
        with patch.dict(
            "os.environ",
            {"WHISPER_TIMEOUT": "120"},
            clear=False,
        ):
            client = WhisperClientPool()
            assert client._timeout == 120

    def test_transcribe_sends_audio_field(self, mocker):
        """Verify the multipart field name is 'audio', not 'file'."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "text": "test",
            "language": "es",
            "duration": 1.0,
        }
        mock_post = mocker.patch("requests.post", return_value=mock_response)

        client = WhisperClientPool()
        client.transcribe(audio_bytes=b"audio data", filename="test.mp3", language="es")

        call_kwargs = mock_post.call_args
        files_sent = call_kwargs.kwargs.get("files") or call_kwargs[1].get("files")
        assert "audio" in files_sent, "Must send file as 'audio', not 'file'"
        assert files_sent["audio"][0] == "test.mp3"
        assert files_sent["audio"][1] == b"audio data"

    def test_transcribe_duration_field_mapping(self, mocker):
        """Verify 'duration' from API response maps to duration_seconds in model."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "text": "test audio",
            "language": "en",
            "duration": 42.7,
        }
        mocker.patch("requests.post", return_value=mock_response)

        client = WhisperClientPool()
        result = client.transcribe(audio_bytes=b"audio", filename="test.mp3")

        assert result.duration_seconds == 42.7, (
            "duration from API response must be mapped to duration_seconds"
        )