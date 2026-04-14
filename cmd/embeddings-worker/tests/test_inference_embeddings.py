"""
Tests for inference embeddings generation in embeddings-worker.

These tests verify the behavior of inference embeddings generation
at the integration level using mocked dependencies.
"""

import json
import os
import sys
import pytest
from unittest.mock import MagicMock, patch, Mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, "/app")


def _setup_mock_modules():
    """Set up mock modules before importing worker."""
    import sys
    from unittest.mock import MagicMock
    import json

    mock_pkg = MagicMock()
    sys.modules['pkg'] = mock_pkg
    sys.modules['pkg.events_python'] = MagicMock()
    sys.modules['pkg.worker_common'] = MagicMock()
    sys.modules['pkg.worker_common.rabbitmq'] = MagicMock()
    sys.modules['app'] = MagicMock()
    sys.modules['app.services'] = MagicMock()
    sys.modules['app.services.embeddings'] = MagicMock()
    sys.modules['torch'] = MagicMock()
    sys.modules['pika'] = MagicMock()
    sys.modules['redis'] = MagicMock()
    sys.modules['requests'] = MagicMock()
    
    # Mock msgpack with real serialization behavior for testing
    mock_msgpack = MagicMock()
    
    def mock_packb(data, **kwargs):
        """Simulate msgpack.packb by JSON serializing then encoding to bytes."""
        return json.dumps(data).encode('utf-8')
    
    def mock_unpackb(data, **kwargs):
        """Simulate msgpack.unpackb by decoding bytes then JSON parsing."""
        return json.loads(data.decode('utf-8'))
    
    mock_msgpack.packb = mock_packb
    mock_msgpack.unpackb = mock_unpackb
    sys.modules['msgpack'] = mock_msgpack


def _create_mock_embeddings_worker():
    """Create EmbeddingsWorker with mocked dependencies."""
    _setup_mock_modules()

    import worker

    mock_redis = MagicMock()
    mock_redis.exists.return_value = False
    mock_redis.get.return_value = None

    worker_obj = worker.EmbeddingsWorker.__new__(worker.EmbeddingsWorker)
    worker_obj.redis_client = mock_redis
    worker_obj.service = MagicMock()
    worker_obj.batch_size = 32
    worker_obj.event_bus = MagicMock()
    worker_obj._jobs_total = MagicMock()
    worker_obj._job_duration = MagicMock()
    worker_obj._stopping = False

    return worker_obj, mock_redis


class TestInferenceEmbeddingsRedisOperations:
    """Test Redis operations for inference embeddings."""

    def test_check_micro_inferences_exist_returns_true(self):
        """When micro_inferences key exists in Redis, returns True."""
        worker, mock_redis = _create_mock_embeddings_worker()
        mock_redis.exists.return_value = True

        result = worker._check_micro_inferences_exist("job-123")

        assert result is True
        mock_redis.exists.assert_called_once_with("orchestrator:job:job-123:micro_inferences")

    def test_check_micro_inferences_exist_returns_false(self):
        """When micro_inferences key does not exist, returns False."""
        worker, mock_redis = _create_mock_embeddings_worker()
        mock_redis.exists.return_value = False

        result = worker._check_micro_inferences_exist("job-123")

        assert result is False

    def test_load_micro_inferences_parses_json(self):
        """Micro-inferences are loaded from Redis and parsed as JSON."""
        worker, mock_redis = _create_mock_embeddings_worker()
        raw_data = [
            {"chunk_id": "chunk_0", "inferences": [{"text": "Inf 1", "confidence": 0.9}]},
            {"chunk_id": "chunk_1", "inferences": [{"text": "Inf 2", "confidence": 0.8}]},
        ]
        mock_redis.get.return_value = json.dumps(raw_data)

        result = worker._load_micro_inferences("job-123")

        assert len(result) == 2
        assert result[0]["chunk_id"] == "chunk_0"
        assert result[1]["chunk_id"] == "chunk_1"
        mock_redis.get.assert_called_once_with("orchestrator:job:job-123:micro_inferences")

    def test_load_micro_inferences_returns_none_on_missing_key(self):
        """When micro_inferences key is missing, returns empty list."""
        worker, mock_redis = _create_mock_embeddings_worker()
        mock_redis.get.return_value = None

        result = worker._load_micro_inferences("job-123")

        assert result == []


class TestInferenceEmbeddingsGeneration:
    """Test inference embeddings generation logic."""

    def test_generate_inference_embeddings_returns_correct_structure(self):
        """Generated embeddings have correct nested dict structure."""
        worker, mock_redis = _create_mock_embeddings_worker()

        mock_embedding_service = MagicMock()
        mock_embedding_service.generate_embeddings.return_value = [
            [0.1, 0.2, 0.3],
            [0.4, 0.5, 0.6],
        ]
        worker.service = mock_embedding_service

        micro_inferences = [
            {
                "chunk_id": "chunk_0",
                "inferences": [
                    {"text": "Inference 1", "confidence": 0.9},
                    {"text": "Inference 2", "confidence": 0.8},
                ],
            }
        ]

        result = worker._generate_inference_embeddings(micro_inferences)

        assert "chunk_0" in result
        assert "inference_0" in result["chunk_0"]
        assert "inference_1" in result["chunk_0"]
        assert result["chunk_0"]["inference_0"] == [0.1, 0.2, 0.3]
        assert result["chunk_0"]["inference_1"] == [0.4, 0.5, 0.6]

    def test_generate_inference_embeddings_multiple_chunks(self):
        """Multiple chunks with inferences are processed correctly."""
        worker, mock_redis = _create_mock_embeddings_worker()

        mock_embedding_service = MagicMock()
        mock_embedding_service.generate_embeddings.return_value = [
            [0.1, 0.2, 0.3],
            [0.4, 0.5, 0.6],
            [0.7, 0.8, 0.9],
        ]
        worker.service = mock_embedding_service

        micro_inferences = [
            {
                "chunk_id": "chunk_0",
                "inferences": [{"text": "Inf 1", "confidence": 0.9}],
            },
            {
                "chunk_id": "chunk_1",
                "inferences": [
                    {"text": "Inf 2", "confidence": 0.8},
                    {"text": "Inf 3", "confidence": 0.7},
                ],
            },
        ]

        result = worker._generate_inference_embeddings(micro_inferences)

        assert "chunk_0" in result
        assert "chunk_1" in result
        assert "inference_0" in result["chunk_0"]
        assert "inference_0" in result["chunk_1"]
        assert "inference_1" in result["chunk_1"]

    def test_empty_inferences_returns_empty_dict(self):
        """Empty inference list returns empty dict."""
        worker, mock_redis = _create_mock_embeddings_worker()

        result = worker._generate_inference_embeddings([])

        assert result == {}

    def test_inference_texts_are_extracted_for_embedding(self):
        """Only the text field of each inference is used for embedding."""
        worker, mock_redis = _create_mock_embeddings_worker()

        texts_captured = []
        mock_embedding_service = MagicMock()
        mock_embedding_service.generate_embeddings.side_effect = lambda texts, **kwargs: texts_captured.append(texts) or [[0.1]]
        worker.service = mock_embedding_service

        micro_inferences = [
            {
                "chunk_id": "chunk_0",
                "inferences": [
                    {"text": "First inference text", "confidence": 0.9, "entity_refs": ["entity1"]},
                    {"text": "Second inference text", "confidence": 0.8, "entity_refs": ["entity2"]},
                ],
            }
        ]

        worker._generate_inference_embeddings(micro_inferences)

        assert len(texts_captured) == 1
        assert texts_captured[0] == ["First inference text", "Second inference text"]


class TestInferenceEmbeddingsStorage:
    """Test storage of inference embeddings in Redis."""

    def test_save_inference_embeddings_uses_msgpack(self):
        """Inference embeddings are stored using MessagePack serialization."""
        import msgpack

        worker, mock_redis = _create_mock_embeddings_worker()
        
        # Mock the pipeline for atomic operations
        mock_pipe = MagicMock()
        mock_redis.pipeline.return_value = mock_pipe

        inference_embeddings = {
            "chunk_0": {
                "inference_0": [0.1, 0.2, 0.3],
            }
        }

        worker._save_inference_embeddings("job-123", inference_embeddings)

        # Verify set was called via pipeline with correct key and serialized value
        set_call = mock_pipe.set.call_args
        key = set_call[0][0]
        value = set_call[0][1]

        assert key == "orchestrator:job:job-123:inference_embeddings"
        unpacked = msgpack.unpackb(value, raw=False)
        assert unpacked["chunk_0"]["inference_0"] == [0.1, 0.2, 0.3]

    def test_save_inference_embeddings_sets_correct_ttl(self):
        """Inference embeddings are stored with appropriate TTL using atomic pipeline."""
        worker, mock_redis = _create_mock_embeddings_worker()
        
        # Mock the pipeline for atomic operations
        mock_pipe = MagicMock()
        mock_redis.pipeline.return_value = mock_pipe

        inference_embeddings = {"chunk_0": {"inference_0": [0.1, 0.2, 0.3]}}

        worker._save_inference_embeddings("job-123", inference_embeddings)

        # Verify pipeline was used for atomicity
        mock_redis.pipeline.assert_called_once()
        assert mock_pipe.set.called
        assert mock_pipe.expire.called
        assert mock_pipe.execute.called
        
        # Verify set call had correct key
        set_call = mock_pipe.set.call_args
        assert set_call[0][0] == "orchestrator:job:job-123:inference_embeddings"
        
        # Verify expire call had correct TTL
        expire_call = mock_pipe.expire.call_args
        assert expire_call[0][0] == "orchestrator:job:job-123:inference_embeddings"
        assert expire_call[0][1] == 86400


class TestBackwardCompatibility:
    """Test backward compatibility when no micro-inferences exist."""

    def test_continues_when_no_micro_inferences(self):
        """Worker continues normally when micro_inferences do not exist."""
        worker, mock_redis = _create_mock_embeddings_worker()
        mock_redis.exists.return_value = False

        has_inferences = worker._check_micro_inferences_exist("job-123")

        assert has_inferences is False

    def test_empty_micro_inferences_does_not_store_embeddings(self):
        """When micro_inferences are empty, no embeddings are stored."""
        worker, mock_redis = _create_mock_embeddings_worker()
        mock_redis.exists.return_value = True
        mock_redis.get.return_value = "[]"

        micro_inferences = worker._load_micro_inferences("job-123")
        if micro_inferences:
            worker._save_inference_embeddings("job-123", worker._generate_inference_embeddings(micro_inferences))

        mock_redis.set.assert_not_called()
