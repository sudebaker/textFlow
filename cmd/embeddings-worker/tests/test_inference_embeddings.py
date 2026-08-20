"""
Tests for inference embeddings generation in embeddings-worker.

The old tests targeted worker APIs that were removed when the feature was
rewritten inline in EmbeddingsWorker.process_message (D3 artifact-store
migration). These tests cover:

1. Unit tests for the shared helper pkg.worker_common.inference_embeddings
   .generate_inference_embeddings.
2. A bounded integration test of EmbeddingsWorker.process_message writing
   inference embeddings as artifact-store refs (no TTL, post-D3).
"""

import json
import sys
from unittest.mock import MagicMock, patch

import msgpack
import pytest

from pkg.worker_common.artifact_store import FSStore
from pkg.worker_common.inference_embeddings import generate_inference_embeddings


class _TensorLike:
    """Mimics a numpy array / torch tensor by exposing tolist()."""

    def __init__(self, values):
        self._values = values

    def tolist(self):
        return list(self._values)


class TestGenerateInferenceEmbeddings:
    """Unit tests for the pure helper generate_inference_embeddings()."""

    def test_returns_embeddings_nested_by_chunk_and_index(self):
        embed_fn = lambda texts: [[0.1, 0.2], [0.3, 0.4]]
        result = generate_inference_embeddings(
            {
                "c0": [
                    {"text": "a", "confidence": 0.9},
                    {"text": "b", "confidence": 0.8},
                ]
            },
            embed_fn,
            MagicMock(),
        )
        assert result == {"c0": {"inference_0": [0.1, 0.2], "inference_1": [0.3, 0.4]}}

    def test_multiple_chunks(self):
        embed_fn = lambda texts: [[0.1]] * len(texts)
        inferences = {
            "c0": [{"text": "a"}],
            "c1": [{"text": "b"}, {"text": "c"}],
        }
        result = generate_inference_embeddings(inferences, embed_fn, MagicMock())
        assert set(result) == {"c0", "c1"}
        assert set(result["c1"]) == {"inference_0", "inference_1"}

    def test_empty_input_returns_empty_dict(self):
        result = generate_inference_embeddings({}, lambda texts: [], MagicMock())
        assert result == {}

    def test_chunk_with_no_inferences_is_skipped(self):
        embed_fn = MagicMock(return_value=[[0.1]])
        result = generate_inference_embeddings(
            {"c0": [], "c1": [{"text": "x"}]}, embed_fn, MagicMock()
        )
        assert result == {"c1": {"inference_0": [0.1]}}
        embed_fn.assert_called_once_with(["x"])

    def test_chunk_with_only_blank_texts_is_skipped(self):
        embed_fn = MagicMock(return_value=[[0.1]])
        result = generate_inference_embeddings(
            {"c0": [{"text": ""}, {"text": None}]}, embed_fn, MagicMock()
        )
        assert result == {}
        embed_fn.assert_not_called()

    def test_tensor_embeddings_converted_via_tolist(self):
        embed_fn = lambda texts: [_TensorLike([0.5, 0.6])]
        result = generate_inference_embeddings(
            {"c0": [{"text": "a"}]}, embed_fn, MagicMock()
        )
        assert result["c0"]["inference_0"] == [0.5, 0.6]

    def test_embed_failure_logs_warning_and_skips_chunk(self):
        logger = MagicMock()
        embed_fn = MagicMock(side_effect=RuntimeError("boom"))
        result = generate_inference_embeddings({"c0": [{"text": "a"}]}, embed_fn, logger)
        assert result == {}
        logger.warning.assert_called_once()


@pytest.fixture
def worker_env(monkeypatch, tmp_path):
    """EmbeddingsWorker with BaseWorker deps, metrics, and torch mocked.

    torch is not installed in the air-gapped test env, and embeddings_worker
    imports it at module level, so it is stubbed before the module loads.
    """
    from pkg.worker_common.artifact_store import FSStore
    from pkg.worker_common.base import BaseWorker

    torch_mock = MagicMock()
    torch_mock.cuda.is_available.return_value = False
    monkeypatch.setitem(sys.modules, "torch", torch_mock)

    mock_redis = MagicMock()
    mock_event_bus = MagicMock()

    with patch.object(BaseWorker, "redis_client", mock_redis), \
         patch.object(BaseWorker, "event_bus", mock_event_bus), \
         patch("pkg.worker_common.base.Counter"), \
         patch("pkg.worker_common.base.Histogram"), \
         patch("pkg.worker_common.base.Gauge"), \
         patch("prometheus_client.Gauge"):
        import embeddings_worker as worker

        store = FSStore(str(tmp_path))
        monkeypatch.setattr(worker, "STORE", store)

        worker_obj = worker.EmbeddingsWorker()
        worker_obj.batch_size = 32

        yield {
            "worker": worker_obj,
            "service": MagicMock(),
            "redis_client": mock_redis,
            "event_bus": mock_event_bus,
            "store": store,
        }


class TestProcessMessageInferenceEmbeddings:
    """Bounded integration of the inference-embeddings branch of process_message."""

    def test_generates_and_stores_inference_embeddings(self, worker_env):
        w = worker_env["worker"]
        service = worker_env["service"]
        service.generate_embeddings.side_effect = [
            [[0.1, 0.2]],  # chunk c0 embeddings
            [[0.7, 0.8]],  # inference_0 embeddings
        ]
        w.service = service

        redis = worker_env["redis_client"]
        redis.exists.return_value = 1
        redis.get.return_value = json.dumps(
            [{"chunk_id": "c0", "inferences": [{"text": "Some inference", "confidence": 0.9}]}]
        )

        result = w.process_message(
            {"job_id": "j1", "chunks": [{"chunk_id": "c0", "text": "hello world"}]}
        )

        assert result["status"] == "success"
        store = worker_env["store"]

        chunks_call = next(
            c for c in redis.set.call_args_list
            if c[0][0] == "orchestrator:job:j1:embeddings"
        )
        assert msgpack.unpackb(store.get(chunks_call[0][1]), raw=False) == {"c0": [0.1, 0.2]}

        ie_call = next(
            c for c in redis.set.call_args_list
            if c[0][0] == "orchestrator:job:j1:inference_embeddings"
        )
        assert msgpack.unpackb(store.get(ie_call[0][1]), raw=False) == {
            "c0": {"inference_0": [0.7, 0.8]}
        }

        redis.hset.assert_any_call(
            "orchestrator:job:j1:steps", "inference_embeddings", "completed"
        )
        worker_env["event_bus"].publish_job_progress.assert_called_with("j1", 40, "embedding")

    def test_no_micro_inferences_skips_inference_embeddings(self, worker_env):
        w = worker_env["worker"]
        service = worker_env["service"]
        service.generate_embeddings.side_effect = [[[0.1, 0.2]]]
        w.service = service

        redis = worker_env["redis_client"]
        redis.exists.return_value = 0

        result = w.process_message(
            {"job_id": "j1", "chunks": [{"chunk_id": "c0", "text": "hello world"}]}
        )

        assert result["status"] == "success"
        assert not any(
            c[0][0] == "orchestrator:job:j1:inference_embeddings"
            for c in redis.set.call_args_list
        )
        worker_env["event_bus"].publish_job_progress.assert_called_with("j1", 33, "embedding")


class TestBatchMetrics:
    """Throughput metrics (spec 33): batch duration histogram + chunk counter."""

    def test_metrics_registered(self, worker_env):
        import embeddings_worker as worker

        assert worker.batch_duration.collect()[0].name == "embeddings_worker_batch_duration_seconds"
        assert worker.chunks_total.collect()[0].samples[0].name == "embeddings_worker_chunks_total"
        assert worker.batch_duration._labelnames == ("batch_size",)
        assert worker.chunks_total._labelnames == ("status",)

    def test_chunk_counter_increments_during_processing(self, worker_env):
        import embeddings_worker as worker

        w = worker_env["worker"]
        service = worker_env["service"]
        service.generate_embeddings.return_value = [[0.1, 0.2]]
        w.service = service
        worker_env["redis_client"].exists.return_value = 0

        before = worker.chunks_total.labels(status="success")._value.get()
        result = w.process_message(
            {"job_id": "j-m", "chunks": [{"chunk_id": "c0", "text": "hello world"}]}
        )
        after = worker.chunks_total.labels(status="success")._value.get()

        assert result["status"] == "success"
        assert after == before + 1
