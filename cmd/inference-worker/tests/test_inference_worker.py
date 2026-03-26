import pytest
import json
import sys
import os
from unittest.mock import Mock, patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from worker import InferenceWorker


class TestInferenceWorker:
    @pytest.fixture
    def worker(self):
        with patch("redis.from_url"):
            return InferenceWorker()

    def test_extract_inferences_success(self, worker):
        """Test successful inference extraction"""
        text = "The property has a value of 500,000 EUR and was built in 2010."
        llm_url = "http://localhost:8000"

        with patch("requests.post") as mock_post:
            mock_response = Mock()
            mock_response.json.return_value = {
                "choices": [{"text": '[{"fact": "Property value is 500,000 EUR", "confidence": 0.95}]'}]
            }
            mock_post.return_value = mock_response

            inferences = worker.extract_inferences(text, llm_url)

            assert len(inferences) == 1
            assert inferences[0]["fact"] == "Property value is 500,000 EUR"
            assert inferences[0]["confidence"] == 0.95
            assert inferences[0]["source"] == "llm"

    def test_extract_inferences_no_llm_url(self, worker):
        """Test with no LLM URL configured"""
        inferences = worker.extract_inferences("Some text", "")
        assert inferences == []

    def test_extract_inferences_llm_failure(self, worker):
        """Test LLM call failure"""
        with patch("requests.post") as mock_post:
            mock_post.side_effect = Exception("Connection failed")
            inferences = worker.extract_inferences("Some text", "http://localhost:8000")
            assert inferences == []
