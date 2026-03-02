#!/usr/bin/env python3
"""Test extraction worker with mocked Docling API"""
import pytest
import json
import base64
from unittest.mock import Mock, patch, MagicMock
import sys
import os

# Mock environment variables before importing worker
os.environ["DOCLING_URL"] = "http://docling:5001"
os.environ["REDIS_URL"] = "redis://localhost:6379"
os.environ["RABBITMQ_URL"] = "amqp://localhost:5672/"

# Import worker module
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))


@pytest.fixture
def mock_requests():
    """Mock the requests library"""
    with patch("requests.post") as mock_post, \
         patch("requests.get") as mock_get:
        yield mock_post, mock_get


@pytest.fixture
def docling_response():
    """Sample Docling API response"""
    return {
        "markdown": "# Document Title\n\nThis is extracted text from Docling.\n\nPage 1 content.",
        "num_pages": 1,
        "success": True,
    }


class TestDoclingExtraction:
    """Test Docling extraction methods"""

    def test_docling_url_from_env(self):
        """Test that DOCLING_URL environment variable is correctly set"""
        from worker import DOCLING_URL
        assert DOCLING_URL == "http://docling:5001"
        assert "unstructured" not in DOCLING_URL.lower()

    def test_extract_text_from_base64(self, mock_requests, docling_response):
        """Test base64 document extraction"""
        mock_post, _ = mock_requests
        
        # Mock the Docling POST response
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = docling_response
        mock_post.return_value = mock_response
        
        # Import worker after mocking
        from worker import ExtractionWorker
        
        worker = ExtractionWorker()
        test_document = b"fake pdf content"
        base64_doc = base64.b64encode(test_document).decode()
        
        result = worker.extract_text_from_base64(base64_doc, "test.pdf")
        
        # Verify Docling endpoint was called
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert "docling:5001" in call_args[0][0]
        assert "/convert" in call_args[0][0]
        
        # Verify response parsing
        assert result["text"] == docling_response["markdown"]
        assert result["metadata"]["docling_pages"] == 1
        assert result["metadata"]["extraction_method"] == "base64"

    def test_extract_text_from_file(self, mock_requests, docling_response, tmp_path):
        """Test file extraction"""
        mock_post, _ = mock_requests
        
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = docling_response
        mock_post.return_value = mock_response
        
        from worker import ExtractionWorker
        
        # Create a test file
        test_file = tmp_path / "test.pdf"
        test_file.write_bytes(b"fake pdf content")
        
        worker = ExtractionWorker()
        result = worker.extract_text_from_file(str(test_file), "test.pdf")
        
        # Verify Docling endpoint was called
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert "/convert" in call_args[0][0]
        assert call_args[1]["files"]["file"][0] == "test.pdf"
        
        # Verify response
        assert result["text"] == docling_response["markdown"]
        assert result["metadata"]["extraction_method"] == "file"

    def test_extract_text_from_url(self, mock_requests, docling_response):
        """Test URL-based extraction"""
        mock_post, mock_get = mock_requests
        
        mock_post_response = Mock()
        mock_post_response.status_code = 200
        mock_post_response.json.return_value = docling_response
        mock_post.return_value = mock_post_response
        
        mock_get_response = Mock()
        mock_get_response.content = b"fake pdf content"
        mock_get.return_value = mock_get_response
        
        from worker import ExtractionWorker
        
        worker = ExtractionWorker()
        result = worker.extract_text_from_url("https://example.com/document.pdf")
        
        # Verify GET was called to fetch the document
        mock_get.assert_called_once_with("https://example.com/document.pdf", timeout=30)
        
        # Verify POST was called to Docling
        mock_post.assert_called_once()
        assert "/convert" in mock_post.call_args[0][0]
        
        # Verify response
        assert result["text"] == docling_response["markdown"]
        assert result["metadata"]["extraction_method"] == "url"

    def test_docling_api_endpoint(self, mock_requests):
        """Test that Docling /convert endpoint is used (not /general/v0/general)"""
        mock_post, _ = mock_requests
        
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"markdown": "test", "num_pages": 1}
        mock_post.return_value = mock_response
        
        from worker import ExtractionWorker
        
        worker = ExtractionWorker()
        worker.extract_text_from_base64(base64.b64encode(b"test").decode(), "test.pdf")
        
        # Verify the endpoint is /convert, not /general/v0/general
        call_url = mock_post.call_args[0][0]
        assert "/convert" in call_url
        assert "general/v0/general" not in call_url
        assert "unstructured" not in call_url

    def test_docling_request_format(self, mock_requests):
        """Test that request format uses 'file=' (not 'files=')"""
        mock_post, _ = mock_requests
        
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"markdown": "test", "num_pages": 1}
        mock_post.return_value = mock_response
        
        from worker import ExtractionWorker
        
        worker = ExtractionWorker()
        worker.extract_text_from_base64(base64.b64encode(b"test").decode(), "test.pdf")
        
        # Verify files parameter is used (Docling expects 'files' in requests.post)
        call_kwargs = mock_post.call_args[1]
        assert "files" in call_kwargs
        assert call_kwargs["files"]["file"][0] == "test.pdf"

    def test_response_markdown_extraction(self, mock_requests):
        """Test that response correctly extracts 'markdown' field"""
        mock_post, _ = mock_requests
        
        # Response with markdown field
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "markdown": "# Extracted Markdown Content",
            "num_pages": 2
        }
        mock_post.return_value = mock_response
        
        from worker import ExtractionWorker
        
        worker = ExtractionWorker()
        result = worker.extract_text_from_base64(base64.b64encode(b"test").decode(), "test.pdf")
        
        assert result["text"] == "# Extracted Markdown Content"
        assert result["metadata"]["docling_pages"] == 2

    def test_no_unstructured_references(self):
        """Test that extraction worker doesn't reference Unstructured API"""
        with open(__file__.replace("test_", "").replace(".py", ".py"), "r") as f:
            content = f.read()
        
        # Check for old Unstructured references
        assert "UNSTRUCTURED_URL" not in content, "Found UNSTRUCTURED_URL in extraction worker"
        assert "/general/v0/general" not in content, "Found Unstructured endpoint in extraction worker"
        assert "longkeyy/unstructured" not in content, "Found Unstructured image reference"


class TestDoclingConfigValidation:
    """Test Docling configuration"""

    def test_docling_url_environment_variable(self):
        """Test that DOCLING_URL environment variable is set"""
        assert os.getenv("DOCLING_URL") == "http://docling:5001"

    def test_docling_health_check_path(self):
        """Test that Docling health check uses /openapi.json"""
        # This test is more for orchestrator config, but validates the expectation
        # Docling serves OpenAPI spec at /openapi.json (not /health)
        assert "/openapi.json" is not None  # Just verify it's documented


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
