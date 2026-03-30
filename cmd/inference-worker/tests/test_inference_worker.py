import pytest
import json
import sys
import os
from unittest.mock import Mock, patch, MagicMock
import requests

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
        
        # Set up discovered model info on the worker instance
        worker.llm_model_id = "qwen3.5-2b"
        worker.llm_max_model_len = 4096

        with patch("worker.LLM_URL", "http://localhost:8000"):
            with patch("requests.post") as mock_post:
                mock_response = Mock()
                mock_response.raise_for_status = Mock()
                # Match /v1/chat/completions response format
                mock_response.json.return_value = {
                    "choices": [{
                        "message": {
                            "content": '[{"text": "Property value is 500000 EUR", "confidence": 0.95, "entity_refs": ["500000 EUR"]}]'
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
                assert inferences[0]["entity_refs"] == ["500000 EUR"]
                assert "entities" not in inferences[0]
                # Old fields must NOT be present
                assert "fact" not in inferences[0]
                assert "source" not in inferences[0]

    def test_discover_model_success(self):
        """Test successful model discovery from /v1/models endpoint"""
        with patch("requests.get") as mock_get:
            mock_response = Mock()
            mock_response.json.return_value = {
                "data": [{
                    "id": "qwen3.5-2b",
                    "max_model_len": 16384,
                }]
            }
            mock_get.return_value = mock_response
            
            model_id, max_len = InferenceWorker._discover_model("http://localhost:8000")
            
            assert model_id == "qwen3.5-2b"
            assert max_len == 16384
            mock_get.assert_called_once_with(
                "http://localhost:8000/v1/models",
                timeout=5,
            )

    def test_discover_model_unreachable(self):
        """Test graceful fallback when vLLM is unreachable"""
        with patch("requests.get") as mock_get:
            mock_get.side_effect = requests.RequestException("Connection refused")
            
            model_id, max_len = InferenceWorker._discover_model("http://localhost:8000")
            
            assert model_id is None
            assert max_len is None

    def test_extract_inferences_uses_discovered_model(self, worker):
        """Test that extract_inferences uses discovered model and dynamic max_tokens"""
        worker.llm_model_id = "qwen3.5-2b"
        worker.llm_max_model_len = 2048
        
        with patch("worker.LLM_URL", "http://localhost:8000"):
            with patch("requests.post") as mock_post:
                mock_response = Mock()
                mock_response.raise_for_status = Mock()
                mock_response.json.return_value = {
                    "choices": [{
                        "message": {
                            "content": '[{"text": "Test fact", "confidence": 0.95, "entities": []}]'
                        }
                    }]
                }
                mock_post.return_value = mock_response
                
                inferences = worker.extract_inferences(
                    chunk_text="Some text",
                    entities=[],
                    source_type="catastro",
                )
                
                # Verify POST payload uses discovered model
                call_kwargs = mock_post.call_args[1]
                assert call_kwargs["json"]["model"] == "qwen3.5-2b"
                # max_tokens should be max(200, 2048-900) = 1148
                assert call_kwargs["json"]["max_tokens"] == 1148

    def test_extract_inferences_no_llm_url(self, worker):
        with patch("worker.LLM_URL", ""):
            inferences = worker.extract_inferences(
                chunk_text="Some text", entities=[], source_type="generico"
            )
            assert inferences == []

    def test_extract_inferences_llm_failure(self, worker):
        """Test that HTTP failures are handled gracefully"""
        # Set up discovered model so the method actually tries to call LLM
        worker.llm_model_id = "qwen3.5-2b"
        worker.llm_max_model_len = 4096
        
        with patch("worker.LLM_URL", "http://localhost:8000"):
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
