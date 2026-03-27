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
        """Test successful inference extraction returns new schema fields"""
        text = "The property has a value of 500,000 EUR and was built in 2010."

        with patch("worker.LLM_URL", "http://localhost:8000"):
            with patch("worker.LLM_MODEL", "test-model"):
                with patch("requests.post") as mock_post:
                    mock_response = Mock()
                    mock_response.raise_for_status = Mock()
                    # Match /v1/chat/completions response format
                    mock_response.json.return_value = {
                        "choices": [{
                            "message": {
                                "content": '[{"text": "Property value is 500000 EUR", "confidence": 0.95, "entities": ["500000 EUR"]}]'
                            }
                        }]
                    }
                    mock_post.return_value = mock_response

                    inferences = worker.extract_inferences(
                        chunk_text=text,
                        entities=[],
                        source_type="catastro",
                    )

                    assert len(inferences) == 1
                    assert inferences[0]["text"] == "Property value is 500000 EUR"
                    assert inferences[0]["confidence"] == 0.95
                    assert inferences[0]["entities"] == ["500000 EUR"]
                    # Old fields must NOT be present
                    assert "fact" not in inferences[0]
                    assert "source" not in inferences[0]

    def test_extract_inferences_no_llm_url(self, worker):
        with patch("worker.LLM_URL", ""):
            inferences = worker.extract_inferences(
                chunk_text="Some text", entities=[], source_type="generico"
            )
            assert inferences == []

    def test_extract_inferences_llm_failure(self, worker):
        with patch("worker.LLM_URL", "http://localhost:8000"):
            with patch("worker.LLM_MODEL", "test-model"):
                with patch("requests.post") as mock_post:
                    mock_post.side_effect = Exception("Connection failed")
                    inferences = worker.extract_inferences(
                        chunk_text="Some text", entities=[], source_type="generico"
                    )
                    assert inferences == []

    def test_process_non_last_chunk_publishes_progress(self, worker):
        """Non-last chunk should publish incremental inference progress"""
        worker.redis_client = Mock()
        worker.event_bus = Mock()

        # remaining=1 → not the last chunk
        worker.redis_client.decr.return_value = 1
        worker.redis_client.rpush.return_value = 1
        worker.redis_client.expire.return_value = True

        ch = Mock()
        method = Mock()
        method.delivery_tag = "tag1"

        message = {
            "job_id": "job-123",
            "chunk_id": 0,
            "chunk_text": "Some text about a property.",
            "entities": [],
            "source_type": "generico",
            "total_chunks": 3,
        }

        with patch("worker.LLM_URL", ""):
            worker.process(ch, method, None, json.dumps(message).encode())

        worker.event_bus.publish_job_inference_chunk_progress.assert_called_once_with(
            "job-123",
            chunks_done=2,   # total_chunks(3) - remaining(1)
            chunks_total=3,
        )
        ch.basic_ack.assert_called_once()
