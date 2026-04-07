import threading
from unittest.mock import patch, MagicMock

import pytest

from pkg.image_client.client import MultimodalLLMClientPool
from pkg.image_client.exceptions import MultimodalLLMServiceError
from pkg.image_client.models import ImageAnalysisResult


class TestMultimodalLLMClientPool:
    """Tests for MultimodalLLMClientPool."""

    def test_default_url(self):
        """Test default URL is used when env var not set."""
        with patch.dict("os.environ", {}, clear=False):
            client = MultimodalLLMClientPool()
            assert "http://multimodal-llm:8000" in client._urls

    def test_custom_urls(self):
        """Test custom URLs from env var."""
        with patch.dict(
            "os.environ",
            {"MULTIMODAL_LLM_URLS": "http://llm-1:8000,http://llm-2:8000"},
            clear=False,
        ):
            client = MultimodalLLMClientPool()
            assert len(client._urls) == 2
            assert client._urls[0] == "http://llm-1:8000"
            assert client._urls[1] == "http://llm-2:8000"

    def test_round_robin_selection(self):
        """Test round-robin URL selection advances index."""
        with patch.dict(
            "os.environ",
            {"MULTIMODAL_LLM_URLS": "http://llm-1:8000,http://llm-2:8000"},
            clear=False,
        ):
            client = MultimodalLLMClientPool()
            urls = []
            for _ in range(4):
                urls.append(client._next_url())
            assert urls[0] == "http://llm-1:8000"
            assert urls[1] == "http://llm-2:8000"
            assert urls[2] == "http://llm-1:8000"
            assert urls[3] == "http://llm-2:8000"

    def test_thread_safe_index(self):
        """Test thread-safe index access."""
        with patch.dict(
            "os.environ",
            {"MULTIMODAL_LLM_URLS": "http://llm-1:8000,http://llm-2:8000"},
            clear=False,
        ):
            client = MultimodalLLMClientPool()
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

    def test_analyze_success(self, mocker):
        """Test successful image analysis response parsing."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "extracted_text": "Invoice #12345",
            "description": "A document showing a table of quarterly revenues",
            "language": "es",
            "confidence": 0.94,
        }

        mocker.patch("requests.post", return_value=mock_response)

        client = MultimodalLLMClientPool()
        result = client.analyze(
            image_bytes=b"fake image",
            filename="test.jpg",
        )

        assert isinstance(result, ImageAnalysisResult)
        assert result.extracted_text == "Invoice #12345"
        assert result.description == "A document showing a table of quarterly revenues"
        assert result.language == "es"
        assert result.confidence == 0.94

    def test_analyze_failover(self, mocker):
        """Test failover: first URL returns 500, second URL succeeds."""
        mock_500 = MagicMock()
        mock_500.status_code = 500

        mock_success = MagicMock()
        mock_success.status_code = 200
        mock_success.json.return_value = {
            "extracted_text": "Analysis OK",
            "language": "en",
        }

        mocker.patch(
            "requests.post",
            side_effect=[mock_500, mock_success],
        )

        with patch.dict(
            "os.environ",
            {
                "MULTIMODAL_LLM_URLS": "http://llm-1:8000,http://llm-2:8000",
                "MULTIMODAL_LLM_MAX_RETRIES": "2",
            },
            clear=False,
        ):
            client = MultimodalLLMClientPool()
            result = client.analyze(
                image_bytes=b"image",
                filename="test.jpg",
            )

            assert result.extracted_text == "Analysis OK"

    def test_analyze_max_retries_exceeded(self, mocker):
        """Test max retries exceeded raises ServiceUnavailableError."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mocker.patch("requests.post", return_value=mock_response)

        with patch.dict(
            "os.environ",
            {"MULTIMODAL_LLM_URLS": "http://llm-1:8000", "MULTIMODAL_LLM_MAX_RETRIES": "1"},
            clear=False,
        ):
            client = MultimodalLLMClientPool()
            with pytest.raises(MultimodalLLMServiceError):
                client.analyze(
                    image_bytes=b"image",
                    filename="test.jpg",
                )

    def test_description_appended_to_text(self, mocker):
        """Test description is stored separately for worker to concatenate.
        
        Note: According to the plan, the worker is responsible for concatenating
        description to extracted_text. The client just returns both fields.
        """
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "extracted_text": "Invoice #12345",
            "description": "Quarterly revenue table",
            "language": "en",
            "confidence": 0.90,
        }

        mocker.patch("requests.post", return_value=mock_response)

        client = MultimodalLLMClientPool()
        result = client.analyze(
            image_bytes=b"image",
            filename="test.jpg",
        )

        assert result.extracted_text == "Invoice #12345"
        assert result.description == "Quarterly revenue table"

    def test_confidence_not_stored(self, mocker):
        """Test confidence is returned in result but not passed to Redis."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "extracted_text": "Text",
            "confidence": 0.85,
        }

        mocker.patch("requests.post", return_value=mock_response)

        client = MultimodalLLMClientPool()
        result = client.analyze(
            image_bytes=b"image",
            filename="test.jpg",
        )

        assert result.confidence == 0.85

    def test_timeout_config(self):
        """Test timeout is read from env."""
        with patch.dict(
            "os.environ",
            {"MULTIMODAL_LLM_TIMEOUT": "60"},
            clear=False,
        ):
            client = MultimodalLLMClientPool()
            assert client._timeout == 60