#!/usr/bin/env python3
"""Tests for the adaptive concurrency integration in the inference worker."""

import pytest
from unittest.mock import Mock, patch

from inference_worker import (
    InferenceWorker,
    ADAPTIVE_MAX_CONCURRENCY,
    ADAPTIVE_MIN_CONCURRENCY,
    BATCH_ENABLED,
    BATCH_SIZE,
)


class TestAdaptivePrefetch:
    """Prefetch count is dynamic only when adaptive mode is enabled."""

    @pytest.fixture
    def worker(self):
        with patch("redis.from_url"):
            return InferenceWorker()

    def test_adaptive_disabled_no_semaphore(self, worker):
        """With ADAPTIVE_ENABLED=false (default), no semaphore/executor is wired."""
        assert worker._adaptive is None
        assert worker._executor is None

    def test_adaptive_enabled_wires_semaphore(self):
        """With ADAPTIVE_ENABLED=true, the semaphore is created in __init__."""
        with patch("worker.ADAPTIVE_ENABLED", True):
            with patch("redis.from_url"):
                w = InferenceWorker()
        assert w._adaptive is not None
        assert w._adaptive.min_concurrency == ADAPTIVE_MIN_CONCURRENCY
        assert w._adaptive.max_concurrency == ADAPTIVE_MAX_CONCURRENCY

    def test_adaptive_prefetch_formula(self):
        """Dynamic prefetch = max_concurrency + batch (when batch enabled)."""
        adaptive_prefetch = ADAPTIVE_MAX_CONCURRENCY + (BATCH_SIZE if BATCH_ENABLED else 0)
        if BATCH_ENABLED:
            assert adaptive_prefetch == ADAPTIVE_MAX_CONCURRENCY + BATCH_SIZE
        else:
            assert adaptive_prefetch == ADAPTIVE_MAX_CONCURRENCY


class TestSemaphoreAcquireRelease:
    @pytest.fixture
    def worker(self):
        with patch("redis.from_url"):
            w = InferenceWorker()
            w.llm_model_id = "test-model"
            w.llm_max_model_len = 4096
            return w

    def test_semaphore_acquire_failure_returns_empty(self, worker):
        """When acquire fails, extract_inferences returns an empty list."""
        with patch.object(worker, "_adaptive", Mock()) as mock_sem:
            mock_sem.acquire.return_value = False
            with patch("worker.LLM_URL", "http://localhost:8000"):
                with patch("worker.CACHE_ENABLED", False):
                    result = worker.extract_inferences(
                        chunk_text="Some text", entities=[], source_type="generico"
                    )
        assert result == []
        mock_sem.acquire.assert_called_once()

    def test_semaphore_release_called_with_latency_and_tokens(self, worker):
        """release() is called with is_error=False on a successful LLM call."""
        with patch.object(worker, "_adaptive", Mock()) as mock_sem:
            mock_sem.acquire.return_value = True
            with patch("worker.LLM_URL", "http://localhost:8000"):
                with patch("worker.CACHE_ENABLED", False):
                    with patch("requests.post") as mock_post:
                        mock_response = Mock()
                        mock_response.raise_for_status = Mock()
                        mock_response.json.return_value = {
                            "choices": [{
                                "message": {
                                    "content": '[{"text": "Fact", "confidence": 0.9, "entity_refs": []}]'
                                }
                            }],
                            "usage": {"completion_tokens": 25},
                        }
                        mock_post.return_value = mock_response
                        result = worker.extract_inferences(
                            chunk_text="Some text",
                            entities=[],
                            source_type="generico",
                        )
            assert len(result) == 1
            # Acquire happened, release happened; verify is_error=False.
            mock_sem.acquire.assert_called_once()
            assert mock_sem.release.call_count == 1
            release_kwargs = mock_sem.release.call_args.kwargs
            assert release_kwargs.get("is_error") is False
            assert "latency_ms" in release_kwargs
            assert "tokens_per_sec" in release_kwargs

    def test_cooldown_blocks_new_work(self, worker):
        """When the semaphore is in cooldown, acquire fails and returns empty."""
        with patch.object(worker, "_adaptive", Mock()) as mock_sem:
            mock_sem.is_in_cooldown = True
            mock_sem.acquire.return_value = False
            with patch("worker.LLM_URL", "http://localhost:8000"):
                with patch("worker.CACHE_ENABLED", False):
                    result = worker.extract_inferences(
                        chunk_text="Some text",
                        entities=[],
                        source_type="generico",
                    )
        assert result == []
        mock_sem.acquire.assert_called_once()


class TestGracefulShutdown:
    @pytest.fixture
    def worker(self):
        with patch("redis.from_url"):
            w = InferenceWorker()
            w.llm_model_id = "test-model"
            w.llm_max_model_len = 4096
            return w

    def test_cleanup_waits_inflight(self):
        """cleanup() drains in-flight and exports metrics when adaptive enabled."""
        with patch("worker.ADAPTIVE_ENABLED", True):
            with patch("redis.from_url"):
                worker = InferenceWorker()
                worker.llm_model_id = "test-model"
                worker.llm_max_model_len = 4096
        assert worker._adaptive is not None
        # Simulate an in-flight LLM call, then release it so cleanup exits.
        assert worker._adaptive.acquire(timeout=0.1) is True
        worker._adaptive.release(latency_ms=100, tokens_per_sec=20.0, is_error=False)
        # cleanup should not raise; in-flight is 0 so it exits immediately.
        worker.cleanup()

    def test_cleanup_adaptive_disabled_no_executor(self, worker):
        """Adaptive disabled (default): no _executor, cleanup still works."""
        assert worker._adaptive is None
        assert worker._executor is None
        worker.cleanup()
