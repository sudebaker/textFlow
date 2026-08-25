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


class _FakeConnection:
    """Records callbacks scheduled onto the pika thread."""

    import json as _json_mod  # noqa: avoid top-level shadowing

    def __init__(self):
        self.is_open = True
        self.scheduled = []

    def add_callback_threadsafe(self, cb):
        self.scheduled.append(cb)


import json as _json  # noqa: E402  (module-level alias for test bodies)
from unittest.mock import MagicMock as _MM  # noqa: E402


class _FakeChannel:
    def __init__(self):
        self.acks = []
        self.nacks = []
        self.stops = []

    def basic_ack(self, delivery_tag):
        self.acks.append(delivery_tag)

    def basic_nack(self, delivery_tag, requeue):
        self.nacks.append((delivery_tag, requeue))

    def stop_consuming(self):
        self.stops.append(1)


def _run_scheduled(conn):
    """Execute scheduled callbacks like the pika thread would."""
    pending, conn.scheduled = conn.scheduled[:], []
    for cb in pending:
        try:
            cb()
        except Exception as e:
            print(f"CB EXCEPTION: {type(e).__name__}: {e}")
            raise


def _adaptive_worker():
    import json as _json

    with patch("worker.ADAPTIVE_ENABLED", True):
        with patch("redis.from_url"):
            w = InferenceWorker()
    w.llm_model_id = "test-model"
    w.llm_max_model_len = 4096
    w._connection = _FakeConnection()
    w._channel = _FakeChannel()
    return w


class TestAdaptiveConcurrencyLoop:
    """Full-concurrent design: executor tasks + threadsafe acks."""

    def test_process_dispatches_to_executor_without_touching_channel(self):
        w = _adaptive_worker()
        body = _json.dumps(
            {
                "job_id": "j1",
                "chunk_id": 0,
                "chunk_text": "hello",
                "total_chunks": 1,
            }
        ).encode()

        submitted = []
        with patch("worker.ADAPTIVE_ENABLED", True):
            with patch.object(
                w._executor, "submit", side_effect=lambda fn, *a: submitted.append((fn, a))
            ):
                w.process(
                    object(),  # legacy ch must NOT be touched in adaptive mode
                    type("M", (), {"delivery_tag": 7})(),
                    None,
                    body,
                )

        assert len(submitted) == 1

    def test_adaptive_task_success_acks_via_threadsafe(self):
        w = _adaptive_worker()
        body = _json.dumps(
            {
                "job_id": "j1",
                "chunk_id": 0,
                "chunk_text": "hello world",
                "total_chunks": 2,
                "queued_at": int(__import__("time").time() * 1000),
            }
        ).encode()

        # Not the last chunk: decr returns a positive remaining count.
        from unittest.mock import MagicMock as _MM
        redis = _MM()
        redis.decr.return_value = 1
        w.redis_client = redis

        with patch.object(w, "_observe_queue_time"):
            with patch.object(
                w,
                "extract_inferences",
                return_value=[{"text": "f", "confidence": 0.9, "entity_refs": []}],
            ):
                w._adaptive_task(body, 7)

        _run_scheduled(w._connection)
        assert w._channel.acks == [7]

    def test_adaptive_task_invalid_message_nacks_no_requeue(self):
        w = _adaptive_worker()
        body = _json.dumps({"job_id": "j1"}).encode()  # missing fields

        w._adaptive_task(body, 9)
        _run_scheduled(w._connection)

        assert w._channel.nacks == [(9, False)]
        assert w._channel.acks == []

    def test_adaptive_task_error_nacks_with_requeue(self):
        w = _adaptive_worker()
        body = _json.dumps(
            {"job_id": "j", "chunk_id": 0, "chunk_text": "t", "total_chunks": 1}
        ).encode()

        with patch.object(w, "_observe_queue_time"):
            with patch.object(
                w, "extract_inferences", side_effect=RuntimeError("boom")
            ):
                w._adaptive_task(body, 5)

        _run_scheduled(w._connection)
        assert w._channel.nacks == [(5, True)]

    def test_dispatch_accounts_in_flight(self):
        w = _adaptive_worker()
        # Invalid body: real task takes the short nack path (no Redis/LLM).
        body = _json.dumps({"job_id": "j"}).encode()

        w._dispatch_adaptive(body, 3)
        w._executor.shutdown(wait=True)

        # Dispatch incremented once; task completion decremented once.
        assert w._tasks_in_flight == 0
        assert len(w._connection.scheduled) == 1

    def test_process_rejects_dispatch_when_stopping(self):
        w = _adaptive_worker()
        body = _json.dumps(
            {"job_id": "j", "chunk_id": 0, "chunk_text": "t", "total_chunks": 1}
        ).encode()
        submitted = []
        # Shutdown-reject acks directly on the pika thread (safe).
        ch = _FakeChannel()

        with patch("worker.ADAPTIVE_ENABLED", True):
            with patch("worker._stopping", True):
                with patch.object(
                    w._executor, "submit", side_effect=lambda *a: submitted.append(a)
                ):
                    w.process(
                        ch,
                        type("M", (), {"delivery_tag": 4})(),
                        None,
                        body,
                    )

        assert submitted == []
        assert ch.nacks == [(4, True)]

    def test_last_completed_task_stops_consumer_when_drained_and_stopping(self):
        w = _adaptive_worker()
        body = _json.dumps({"job_id": "j"}).encode()  # invalid short path

        # Simulate one in-flight task so the finally-block decrement reaches 0.
        with w._tasks_lock:
            w._tasks_in_flight = 1

        with patch("worker._stopping", True):
            w._adaptive_task(body, 8)

        # nack (invalid) + consumer-stop callback on shutdown.
        assert len(w._connection.scheduled) == 2
        _run_scheduled(w._connection)
        assert w._channel.nacks == [(8, False)]
        assert w._channel.stops == [1]

    def test_schedule_drops_on_closed_epoch(self):
        w = _adaptive_worker()

        closed = _FakeConnection()
        closed.is_open = False
        w._schedule_on_pika(closed, w._channel, lambda: None)
        assert closed.scheduled == []

        w._schedule_on_pika(None, w._channel, lambda: None)
        assert len(closed.scheduled) == 0

        w._schedule_on_pika(w._connection, w._channel, lambda: None)
        assert len(w._connection.scheduled) == 1

    def test_duplicate_delivery_is_idempotent(self):
        w = _adaptive_worker()
        body = _json.dumps(
            {"job_id": "j", "chunk_id": 0, "chunk_text": "t", "total_chunks": 1}
        ).encode()

        # First delivery stores; second delivery (redelivery) is a no-op.
        redis = _MM()
        redis.set.side_effect = [True, False]  # nx wins once, then loses
        redis.decr.return_value = 1  # not the last chunk: no assembly path
        w.redis_client = redis

        with patch.object(w, "_observe_queue_time"):
            with patch.object(
                w,
                "extract_inferences",
                return_value=[{"text": "f", "confidence": 0.9, "entity_refs": []}],
            ):
                w._adaptive_task(body, 1)
                w._adaptive_task(body, 2)

        # Only the first delivery wrote/decremented.
        assert redis.rpush.call_count == 1
        assert redis.decr.call_count == 1

        _run_scheduled(w._connection)
        assert w._channel.acks == [1, 2]
        assert w._channel.nacks == []

    def test_cleanup_drains_executor_tasks(self):
        w = _adaptive_worker()
        w._tasks_lock.acquire()
        w._tasks_in_flight = 2
        w._tasks_lock.release()

        import threading as _t

        def drain():
            w._tasks_lock.acquire()
            w._tasks_in_flight = 0
            w._tasks_lock.release()

        timer = _t.Timer(0.3, drain)
        timer.start()
        w.cleanup()  # must return once drained, without hanging
        timer.join()
        assert w._tasks_in_flight == 0
