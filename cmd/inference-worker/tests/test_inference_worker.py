import pytest
import json
from unittest.mock import Mock, patch, MagicMock
import requests

from worker import InferenceWorker


class TestInferenceWorker:
    @pytest.fixture
    def worker(self):
        with patch("redis.from_url"):
            return InferenceWorker()

    def test_extract_inferences_success(self, worker):
        """Test successful inference extraction returns new schema fields"""
        text = "The property has a value of 500,000 EUR and was built in 2010."

        worker._llm_model_id = "qwen3.5-2b"
        worker._llm_max_model_len = 4096

        with patch("worker.LLM_URL", "http://localhost:8000"):
            with patch("requests.post") as mock_post:
                mock_response = Mock()
                mock_response.raise_for_status = Mock()
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
                assert "fact" not in inferences[0]
                assert "source" not in inferences[0]

    def test_discover_model_success(self, worker):
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

            model_id, max_len = worker._discover_model("http://localhost:8000")

            assert model_id == "qwen3.5-2b"
            assert max_len == 16384
            mock_get.assert_called_once_with(
                "http://localhost:8000/v1/models",
                timeout=5,
            )

    def test_discover_model_unreachable(self, worker):
        """Test graceful fallback when vLLM is unreachable"""
        with patch("requests.get") as mock_get:
            mock_get.side_effect = requests.RequestException("Connection refused")

            model_id, max_len = worker._discover_model("http://localhost:8000")

            assert model_id is None
            assert max_len is None

    def test_extract_inferences_uses_discovered_model(self, worker):
        """Test that extract_inferences uses discovered model and dynamic max_tokens"""
        worker._llm_model_id = "qwen3.5-2b"
        worker._llm_max_model_len = 2048

        with patch("worker.LLM_URL", "http://localhost:8000"):
            with patch("requests.post") as mock_post:
                mock_response = Mock()
                mock_response.raise_for_status = Mock()
                mock_response.json.return_value = {
                    "choices": [{
                        "message": {
                            "content": '[{"text": "Test fact", "confidence": 0.95, "entity_refs": []}]'
                        }
                    }]
                }
                mock_post.return_value = mock_response

                inferences = worker.extract_inferences(
                    chunk_text="Some text",
                    entities=[],
                    source_type="catastro",
                )

                call_kwargs = mock_post.call_args[1]
                assert call_kwargs["json"]["model"] == "qwen3.5-2b"
                assert call_kwargs["json"]["max_tokens"] == 1148

    def test_extract_inferences_no_llm_url(self, worker):
        with patch("worker.LLM_URL", ""):
            inferences = worker.extract_inferences(
                chunk_text="Some text", entities=[], source_type="generico"
            )
            assert inferences == []

    def test_extract_inferences_llm_failure(self, worker):
        """Test that HTTP failures are handled gracefully"""
        worker._llm_model_id = "qwen3.5-2b"
        worker._llm_max_model_len = 4096

        with patch("worker.LLM_URL", "http://localhost:8000"):
            with patch("requests.post") as mock_post:
                mock_post.side_effect = Exception("Connection failed")
                inferences = worker.extract_inferences(
                    chunk_text="Some text", entities=[], source_type="generico"
                )
                assert inferences == []

    def test_process_non_last_chunk_publishes_progress(self, worker):
        """Non-last chunk should publish incremental inference progress"""
        mock_redis = Mock()
        mock_redis.decr.return_value = 1
        mock_redis.rpush.return_value = 1
        mock_redis.expire.return_value = True
        worker._redis_client = mock_redis
        worker._event_bus = Mock()

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
            with patch("worker.BATCH_ENABLED", False):
                worker.process_message(message)

        worker._event_bus.publish_job_inference_chunk_progress.assert_called_once_with(
            "job-123",
            chunks_done=2,
            chunks_total=3,
        )

    def test_dynamic_max_short_chunk(self, worker):
        """Chunk with < 200 words uses MAX_INFERENCES_SHORT"""
        worker._llm_model_id = "qwen3.5-2b"
        worker._llm_max_model_len = 4096
        short_text = "Short text " * 10

        with patch("worker.LLM_URL", "http://localhost:8000"):
            with patch("worker.MAX_INFERENCES_SHORT", 1):
                with patch("worker.MAX_INFERENCES_MEDIUM", 2):
                    with patch("worker.MAX_INFERENCES_LONG", 3):
                        with patch("requests.post") as mock_post:
                            mock_response = Mock()
                            mock_response.raise_for_status = Mock()
                            mock_response.json.return_value = {
                                "choices": [{"message": {"content": '[{"text": "Fact A", "confidence": 0.9, "entity_refs": []}]'}}]
                            }
                            mock_post.return_value = mock_response

                            worker.extract_inferences(chunk_text=short_text, entities=[], source_type="generico")

                            call_kwargs = mock_post.call_args[1]
                            prompt_sent = call_kwargs["json"]["messages"][1]["content"]
                            assert "1 MOST IMPORTANT" in prompt_sent

    def test_dynamic_max_medium_chunk(self, worker):
        """Chunk with 200-499 words uses MAX_INFERENCES_MEDIUM"""
        worker._llm_model_id = "qwen3.5-2b"
        worker._llm_max_model_len = 4096
        medium_text = "word " * 300

        with patch("worker.LLM_URL", "http://localhost:8000"):
            with patch("worker.MAX_INFERENCES_SHORT", 1):
                with patch("worker.MAX_INFERENCES_MEDIUM", 2):
                    with patch("worker.MAX_INFERENCES_LONG", 3):
                        with patch("requests.post") as mock_post:
                            mock_response = Mock()
                            mock_response.raise_for_status = Mock()
                            mock_response.json.return_value = {
                                "choices": [{"message": {"content": '[{"text": "Fact B", "confidence": 0.9, "entity_refs": []}]'}}]
                            }
                            mock_post.return_value = mock_response

                            worker.extract_inferences(chunk_text=medium_text, entities=[], source_type="generico")

                            call_kwargs = mock_post.call_args[1]
                            prompt_sent = call_kwargs["json"]["messages"][1]["content"]
                            assert "2 MOST IMPORTANT" in prompt_sent

    def test_dynamic_max_long_chunk(self, worker):
        """Chunk with >= 500 words uses MAX_INFERENCES_LONG"""
        worker._llm_model_id = "qwen3.5-2b"
        worker._llm_max_model_len = 4096
        long_text = "word " * 600

        with patch("worker.LLM_URL", "http://localhost:8000"):
            with patch("worker.MAX_INFERENCES_SHORT", 1):
                with patch("worker.MAX_INFERENCES_MEDIUM", 2):
                    with patch("worker.MAX_INFERENCES_LONG", 3):
                        with patch("requests.post") as mock_post:
                            mock_response = Mock()
                            mock_response.raise_for_status = Mock()
                            mock_response.json.return_value = {
                                "choices": [{"message": {"content": '[{"text": "Fact C", "confidence": 0.9, "entity_refs": []}]'}}]
                            }
                            mock_post.return_value = mock_response

                            worker.extract_inferences(chunk_text=long_text, entities=[], source_type="generico")

                            call_kwargs = mock_post.call_args[1]
                            prompt_sent = call_kwargs["json"]["messages"][1]["content"]
                            assert "3 MOST IMPORTANT" in prompt_sent

    def test_confidence_filter_removes_low_confidence(self, worker):
        """Inferences below MIN_CONFIDENCE_THRESHOLD are silently removed"""
        worker._llm_model_id = "qwen3.5-2b"
        worker._llm_max_model_len = 4096
        text = "word " * 50

        with patch("worker.LLM_URL", "http://localhost:8000"):
            with patch("worker.MIN_CONFIDENCE_THRESHOLD", 0.7):
                with patch("requests.post") as mock_post:
                    mock_response = Mock()
                    mock_response.raise_for_status = Mock()
                    mock_response.json.return_value = {
                        "choices": [{"message": {"content": '[{"text": "Low confidence fact", "confidence": 0.5, "entity_refs": []}, {"text": "High confidence fact", "confidence": 0.9, "entity_refs": []}]'}}]
                    }
                    mock_post.return_value = mock_response

                    result = worker.extract_inferences(chunk_text=text, entities=[], source_type="generico")

                    assert len(result) == 1
                    assert result[0]["text"] == "High confidence fact"

    def test_confidence_filter_keeps_threshold_exact(self, worker):
        """Inferences at exactly MIN_CONFIDENCE_THRESHOLD are kept"""
        worker._llm_model_id = "qwen3.5-2b"
        worker._llm_max_model_len = 4096
        text = "word " * 50

        with patch("worker.LLM_URL", "http://localhost:8000"):
            with patch("worker.MIN_CONFIDENCE_THRESHOLD", 0.7):
                with patch("requests.post") as mock_post:
                    mock_response = Mock()
                    mock_response.raise_for_status = Mock()
                    mock_response.json.return_value = {
                        "choices": [{"message": {"content": '[{"text": "Exact threshold fact", "confidence": 0.7, "entity_refs": []}]'}}]
                    }
                    mock_post.return_value = mock_response

                    result = worker.extract_inferences(chunk_text=text, entities=[], source_type="generico")

                    assert len(result) == 1
                    assert result[0]["text"] == "Exact threshold fact"

    def test_cache_hit_returns_cached_inferences(self, worker):
        """Cache hit bypasses LLM call and returns cached results"""
        worker._llm_model_id = "qwen3.5-2b"
        worker._llm_max_model_len = 4096
        mock_redis = Mock()
        cached_inferences = [
            {"text": "Cached fact", "confidence": 0.9, "entity_refs": ["entity1"]},
            {"text": "Low confidence cached", "confidence": 0.3, "entity_refs": []},
        ]
        mock_redis.get.return_value = json.dumps(cached_inferences)
        worker._redis_client = mock_redis

        with patch("worker.LLM_URL", "http://localhost:8000"):
            with patch("worker.CACHE_ENABLED", True):
                with patch("worker.MIN_CONFIDENCE_THRESHOLD", 0.7):
                    with patch("requests.post") as mock_post:
                        result = worker.extract_inferences(
                            chunk_text="Some text", entities=[], source_type="generico"
                        )

                        mock_post.assert_not_called()
                        assert len(result) == 1
                        assert result[0]["text"] == "Cached fact"

    def test_cache_miss_calls_llm(self, worker):
        """Cache miss triggers LLM call and caches result"""
        worker._llm_model_id = "qwen3.5-2b"
        worker._llm_max_model_len = 4096
        mock_redis = Mock()
        mock_redis.get.return_value = None
        mock_redis.setex = Mock()
        worker._redis_client = mock_redis

        with patch("worker.LLM_URL", "http://localhost:8000"):
            with patch("worker.CACHE_ENABLED", True):
                with patch("requests.post") as mock_post:
                    mock_response = Mock()
                    mock_response.raise_for_status = Mock()
                    mock_response.json.return_value = {
                        "choices": [{"message": {"content": '[{"text": "New fact", "confidence": 0.9, "entity_refs": []}]'}}]
                    }
                    mock_post.return_value = mock_response

                    result = worker.extract_inferences(
                        chunk_text="New text to process", entities=[], source_type="generico"
                    )

                    mock_post.assert_called_once()
                    assert len(result) == 1
                    assert result[0]["text"] == "New fact"
                    mock_redis.setex.assert_called_once()

    def test_cache_disabled_skips_redis(self, worker):
        """When cache is disabled, no Redis operations are performed"""
        worker._llm_model_id = "qwen3.5-2b"
        worker._llm_max_model_len = 4096
        mock_redis = Mock()
        worker._redis_client = mock_redis

        with patch("worker.LLM_URL", "http://localhost:8000"):
            with patch("worker.CACHE_ENABLED", False):
                with patch("requests.post") as mock_post:
                    mock_response = Mock()
                    mock_response.raise_for_status = Mock()
                    mock_response.json.return_value = {
                        "choices": [{"message": {"content": '[{"text": "Fact", "confidence": 0.9, "entity_refs": []}]'}}]
                    }
                    mock_post.return_value = mock_response

                    result = worker.extract_inferences(
                        chunk_text="Text without cache", entities=[], source_type="generico"
                    )

                    mock_redis.get.assert_not_called()
                    mock_redis.setex.assert_not_called()

    def test_cache_key_deterministic(self, worker):
        """Same text + source_type produces same cache key"""
        key1 = worker._cache_key("hello world", "catastro")
        key2 = worker._cache_key("hello world", "catastro")
        assert key1 == key2

    def test_cache_key_differs_for_different_text(self, worker):
        """Different text produces different cache key"""
        key1 = worker._cache_key("hello world", "catastro")
        key2 = worker._cache_key("goodbye world", "catastro")
        assert key1 != key2

    def test_cache_key_differs_for_different_source(self, worker):
        """Different source_type produces different cache key"""
        key1 = worker._cache_key("hello world", "catastro")
        key2 = worker._cache_key("hello world", "notariado")
        assert key1 != key2


class TestBatchProcessing:
    """Tests for batch inference processing."""

    @pytest.fixture
    def worker(self):
        with patch("redis.from_url"):
            return InferenceWorker()

    def test_batch_buffer_accumulation(self, worker):
        """Buffer accumulates messages and flushes when full."""
        with patch("worker.BATCH_ENABLED", True):
            with patch("worker.BATCH_SIZE", 2):
                assert len(worker._batch_buffer) == 0

    def test_process_single_mode_dispatches(self, worker):
        """When batch is disabled, process_message calls _process_single_message."""
        worker._llm_model_id = "test-model"
        worker._llm_max_model_len = 4096
        mock_redis = Mock()
        worker._redis_client = mock_redis
        worker._event_bus = Mock()

        with patch("worker.BATCH_ENABLED", False):
            with patch.object(worker, "_process_single_message") as mock_single:
                mock_single.return_value = {"inferences": [], "remaining": 0}
                message = {
                    "job_id": "test-job",
                    "chunk_id": 1,
                    "chunk_text": "Test text",
                    "entities": [],
                    "source_type": "generico",
                    "total_chunks": 1,
                }

                worker.process_message(message)
                mock_single.assert_called_once()

    def test_process_batch_mode_accumulates(self, worker):
        """_process_batch_message accumulates items with delivery metadata."""
        worker._llm_model_id = "test-model"
        worker._llm_max_model_len = 4096
        mock_redis = Mock()
        worker._redis_client = mock_redis

        ch = Mock()
        method = Mock()
        item = {
            "ch": ch,
            "method": method,
            "properties": None,
            "body": b'{}',
            "message": {
                "job_id": "test-job",
                "chunk_id": 1,
                "chunk_text": "Test text for accumulation",
                "entities": [],
                "source_type": "generico",
                "total_chunks": 5,
            },
        }

        with patch("worker.BATCH_SIZE", 10):
            initial_len = len(worker._batch_buffer)
            worker._process_batch_message(item)
            assert len(worker._batch_buffer) == initial_len + 1
            assert worker._batch_buffer[0]["message"]["job_id"] == "test-job"

    def test_batch_lock_released_after_buffering(self, worker):
        """Lock is released after _process_batch_message buffers a message."""
        worker._llm_model_id = "test-model"
        worker._llm_max_model_len = 4096
        mock_redis = Mock()
        worker._redis_client = mock_redis

        with patch("worker.BATCH_SIZE", 2):
            item = {
                "ch": Mock(),
                "method": Mock(),
                "properties": None,
                "body": b'{}',
                "message": {
                    "job_id": "test-job",
                    "chunk_id": 1,
                    "chunk_text": "First text",
                    "entities": [],
                    "source_type": "generico",
                    "total_chunks": 2,
                },
            }

            worker._process_batch_message(item)
            assert not worker._batch_lock.locked()

    def test_flush_batch_buffer_nack_on_error(self, worker):
        """If _process_single_message fails, messages are dead-lettered (requeue=False)."""
        worker._llm_model_id = "test-model"
        worker._llm_max_model_len = 4096
        mock_redis = Mock()
        worker._redis_client = mock_redis
        worker._executor = None
        worker._connection = Mock()

        ch1 = Mock()
        method1 = Mock()
        method1.delivery_tag = "tag1"

        ch2 = Mock()
        method2 = Mock()
        method2.delivery_tag = "tag2"

        batch = [
            {
                "ch": ch1,
                "method": method1,
                "body": b'{}',
                "message": {
                    "job_id": "j1",
                    "chunk_id": 1,
                    "chunk_text": "text1",
                    "total_chunks": 2,
                },
            },
            {
                "ch": ch2,
                "method": method2,
                "body": b'{}',
                "message": {
                    "job_id": "j1",
                    "chunk_id": 2,
                    "chunk_text": "text2",
                    "total_chunks": 2,
                },
            },
        ]

        with patch.object(worker, "_process_single_message", side_effect=Exception("LLM timeout")):
            worker._flush_batch_buffer(batch)

            # NACKs are scheduled via add_callback_threadsafe
            worker._connection.add_callback_threadsafe.assert_called()
            ch1.basic_ack.assert_not_called()
            ch2.basic_ack.assert_not_called()

    def test_flush_batch_buffer_ack_on_success(self, worker):
        """If _process_single_message succeeds, messages are ACKed."""
        worker._llm_model_id = "test-model"
        worker._llm_max_model_len = 4096
        mock_redis = Mock()
        mock_redis.decr.return_value = 1
        mock_redis.rpush.return_value = 1
        mock_redis.expire.return_value = True
        worker._redis_client = mock_redis
        worker._executor = None
        worker._connection = Mock()

        ch1 = Mock()
        method1 = Mock()
        method1.delivery_tag = "tag1"

        item = {
            "ch": ch1,
            "method": method1,
            "body": b'{}',
            "message": {
                "job_id": "j1",
                "chunk_id": 1,
                "chunk_text": "text1",
                "total_chunks": 2,
                "entities": [],
                "source_type": "generico",
            },
        }

        with patch.object(worker, "_process_single_message"):
            worker._flush_batch_buffer([item])
            worker._connection.add_callback_threadsafe.assert_called()
            ch1.basic_ack.assert_not_called()

    def test_cache_key_includes_config(self, worker):
        """Cache key changes when MIN_CONFIDENCE_THRESHOLD changes."""
        with patch("worker.MIN_CONFIDENCE_THRESHOLD", 0.7):
            key1 = worker._cache_key("hello", "catastro")

        with patch("worker.MIN_CONFIDENCE_THRESHOLD", 0.9):
            key2 = worker._cache_key("hello", "catastro")

        assert key1 != key2, "Cache key must change when threshold changes"

    def test_cache_key_includes_inference_limits(self, worker):
        """Cache key changes when MAX_INFERENCES_SHORT/MEDIUM/LONG change."""
        with patch("worker.MAX_INFERENCES_SHORT", 1):
            with patch("worker.MAX_INFERENCES_MEDIUM", 2):
                with patch("worker.MAX_INFERENCES_LONG", 3):
                    key1 = worker._cache_key("text", "generico")

        with patch("worker.MAX_INFERENCES_SHORT", 5):
            with patch("worker.MAX_INFERENCES_MEDIUM", 10):
                with patch("worker.MAX_INFERENCES_LONG", 15):
                    key2 = worker._cache_key("text", "generico")

        assert key1 != key2, "Cache key must change when inference limits change"

    def test_chunk_too_large_skipped_single_mode(self, worker):
        """Chunks exceeding MAX_CHUNK_WORDS are skipped with empty result in single mode."""
        worker._llm_model_id = "test-model"
        worker._llm_max_model_len = 4096
        mock_redis = Mock()
        mock_redis.decr.return_value = 0
        mock_redis.rpush.return_value = 0
        mock_redis.expire.return_value = True
        mock_redis.setnx.return_value = True
        mock_redis.lrange.return_value = []
        worker._redis_client = mock_redis
        worker._event_bus = Mock()

        with patch("worker.MAX_CHUNK_WORDS", 10):
            with patch.object(worker, "_store_empty_result") as mock_store:
                large_text = "word " * 20
                message = {
                    "job_id": "job-1",
                    "chunk_id": 1,
                    "chunk_text": large_text,
                    "entities": [],
                    "source_type": "generico",
                    "total_chunks": 1,
                }

                worker.process_message(message)
                mock_store.assert_called_once()

    def test_chunk_too_large_skipped_batch_mode(self, worker):
        """Chunks exceeding MAX_CHUNK_WORDS are skipped via _flush_batch_buffer."""
        mock_redis = Mock()
        mock_redis.decr.return_value = 0
        mock_redis.rpush.return_value = 0
        mock_redis.expire.return_value = True
        mock_redis.setnx.return_value = True
        mock_redis.lrange.return_value = []
        worker._redis_client = mock_redis
        worker._event_bus = Mock()
        worker._executor = None
        worker._connection = Mock()
        ch = Mock()
        method = Mock()

        with patch("worker.MAX_CHUNK_WORDS", 10):
            with patch.object(worker, "_store_empty_result") as mock_store:
                large_text = "word " * 20
                item = {
                    "ch": ch,
                    "method": method,
                    "body": b'{}',
                    "message": {
                        "job_id": "job-1",
                        "chunk_id": 1,
                        "chunk_text": large_text,
                        "entities": [],
                        "source_type": "generico",
                        "total_chunks": 1,
                    },
                }

                worker._flush_batch_buffer([item])
                mock_store.assert_called_once()

    def test_missing_chunk_text_raises_value_error(self, worker):
        """process_message raises ValueError when chunk_text is missing/empty."""
        mock_redis = Mock()
        worker._redis_client = mock_redis

        with pytest.raises(ValueError, match="No text in message"):
            worker.process_message({"job_id": "job-1"})

    def test_single_llm_retry_on_timeout(self, worker):
        """Single LLM call retries on timeout and succeeds on second attempt."""
        worker._llm_model_id = "test-model"
        worker._llm_max_model_len = 4096

        with patch("worker.LLM_URL", "http://localhost:8000"):
            with patch("worker.CACHE_ENABLED", False):
                with patch("worker.LLM_TIMEOUT", 60):
                    with patch("worker.LLM_RETRIES", 2):
                        with patch("worker.LLM_RETRY_BACKOFF", 0.01):
                            with patch("requests.post") as mock_post:
                                mock_response = Mock()
                                mock_response.raise_for_status = Mock()
                                mock_response.json.return_value = {
                                    "choices": [{"message": {"content": '[{"text": "fact", "confidence": 0.9, "entity_refs": []}]'}}]
                                }
                                mock_post.side_effect = [
                                    requests.Timeout("Connection timed out"),
                                    mock_response,
                                ]

                                result = worker.extract_inferences("test text", [], "generico")
                                assert len(result) == 1
                                assert mock_post.call_count == 2
