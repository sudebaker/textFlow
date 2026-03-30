"""Unit tests for the new finalize_job() output structure."""
import json
import pytest
import sys
import os
import time
import msgpack
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))

from worker import CompletionWorker


@pytest.fixture
def worker():
    with patch("redis.from_url"):
        w = CompletionWorker()
        w.redis_client = MagicMock()
        w.redis_raw = MagicMock()
        return w


def _make_redis_pipeline(chunks, entities_raw, embeddings_dict, micro_inferences=None):
    """Build the tuple returned by pipe.execute() in finalize_job()."""
    meta = {"created_at": str(int(time.time()))}
    status_data = {"status": "processing"}
    text = "sample text"
    doc_meta = json.dumps({"mime_type": "application/pdf"})
    txt_meta = json.dumps({"language": "es"})
    chunks_json = json.dumps(chunks)
    entities_raw_json = json.dumps(entities_raw)
    source_classification_json = None
    micro_inferences_json = json.dumps(micro_inferences) if micro_inferences else None
    return (
        meta, status_data, text, doc_meta, txt_meta,
        chunks_json, entities_raw_json,
        source_classification_json, micro_inferences_json,
    )


def test_embeddings_nested_inside_chunks(worker):
    """Embeddings must be inside each chunk, not at top level."""
    chunks = [{"chunk_id": "chunk_000", "text": "hello"}]
    entities_raw = []
    embeddings_dict = {"chunk_000": [0.1, 0.2, 0.3]}

    pipe = MagicMock()
    pipe.execute.return_value = _make_redis_pipeline(chunks, entities_raw, embeddings_dict)
    worker.redis_client.pipeline.return_value = pipe
    worker.redis_raw.get.return_value = msgpack.packb(embeddings_dict, use_bin_type=True)
    worker.redis_client.set = MagicMock()
    worker.redis_client.hset = MagicMock()

    with patch.object(worker, "save_results_to_file"), patch.object(worker, "send_webhook"), \
         patch.object(worker.event_bus, "publish_job_completed"):
        worker.finalize_job("job_abc")

    saved = json.loads(worker.redis_client.set.call_args_list[-1][0][1])

    assert "embeddings" not in saved, "Top-level 'embeddings' key must be removed"
    chunk = saved["chunks"][0]
    assert chunk["embeddings"] == [0.1, 0.2, 0.3]


def test_entities_dict_with_ids(worker):
    """Top-level entities must be a dict keyed by entity_id."""
    chunks = [{"chunk_id": "chunk_000", "text": "hello"}]
    entities_raw = [
        {"label": "PER", "text": "María", "confidence": 0.9, "chunk_id": "chunk_000", "entity_id": "abc000000001"},
        {"label": "PER", "text": "María", "confidence": 0.8, "chunk_id": "chunk_000", "entity_id": "abc000000001"},
    ]

    pipe = MagicMock()
    pipe.execute.return_value = _make_redis_pipeline(chunks, entities_raw, {})
    worker.redis_client.pipeline.return_value = pipe
    worker.redis_raw.get.return_value = None
    worker.redis_client.set = MagicMock()
    worker.redis_client.hset = MagicMock()

    with patch.object(worker, "save_results_to_file"), patch.object(worker, "send_webhook"), \
         patch.object(worker.event_bus, "publish_job_completed"):
        worker.finalize_job("job_abc")

    saved = json.loads(worker.redis_client.set.call_args_list[-1][0][1])

    assert isinstance(saved["entities"], dict), "entities must be a dict"
    assert "abc000000001" in saved["entities"]
    assert saved["entities"]["abc000000001"]["confidence"] == 0.9  # highest wins
    assert len(saved["entities"]) == 1  # deduplicated


def test_inferences_nested_in_chunks(worker):
    """Inferences must appear inside each chunk, not at top level."""
    chunks = [{"chunk_id": "chunk_000", "text": "hello"}]
    entities_raw = []
    micro_inferences = [
        {"chunk_id": "chunk_000", "inferences": [
            {"text": "fact one", "confidence": 0.9, "entity_refs": ["María"]}
        ]}
    ]

    pipe = MagicMock()
    pipe.execute.return_value = _make_redis_pipeline(chunks, entities_raw, {}, micro_inferences)
    worker.redis_client.pipeline.return_value = pipe
    worker.redis_raw.get.return_value = None
    worker.redis_client.set = MagicMock()
    worker.redis_client.hset = MagicMock()

    with patch.object(worker, "save_results_to_file"), patch.object(worker, "send_webhook"), \
         patch.object(worker.event_bus, "publish_job_completed"):
        worker.finalize_job("job_abc")

    saved = json.loads(worker.redis_client.set.call_args_list[-1][0][1])

    assert "micro_inferences" not in saved, "Top-level micro_inferences must be removed"
    chunk = saved["chunks"][0]
    assert "inferences" in chunk
    assert chunk["inferences"][0]["text"] == "fact one"
    assert chunk["inferences"][0]["entity_refs"] == ["María"]


def test_entity_ids_in_chunks(worker):
    """Each chunk must have entity_ids list referencing global entity dict."""
    chunks = [{"chunk_id": "chunk_000", "text": "hello"}]
    entities_raw = [
        {"label": "PER", "text": "María", "confidence": 0.9, "chunk_id": "chunk_000", "entity_id": "abc000000001"},
    ]

    pipe = MagicMock()
    pipe.execute.return_value = _make_redis_pipeline(chunks, entities_raw, {})
    worker.redis_client.pipeline.return_value = pipe
    worker.redis_raw.get.return_value = None
    worker.redis_client.set = MagicMock()
    worker.redis_client.hset = MagicMock()

    with patch.object(worker, "save_results_to_file"), patch.object(worker, "send_webhook"), \
         patch.object(worker.event_bus, "publish_job_completed"):
        worker.finalize_job("job_abc")

    saved = json.loads(worker.redis_client.set.call_args_list[-1][0][1])
    chunk = saved["chunks"][0]
    assert "entity_ids" in chunk
    assert "abc000000001" in chunk["entity_ids"]


def test_no_top_level_micro_inferences_key(worker):
    """micro_inferences must NOT appear at top level."""
    chunks = [{"chunk_id": "chunk_000", "text": "hello"}]
    pipe = MagicMock()
    pipe.execute.return_value = _make_redis_pipeline(chunks, [], {})
    worker.redis_client.pipeline.return_value = pipe
    worker.redis_raw.get.return_value = None
    worker.redis_client.set = MagicMock()
    worker.redis_client.hset = MagicMock()

    with patch.object(worker, "save_results_to_file"), patch.object(worker, "send_webhook"), \
         patch.object(worker.event_bus, "publish_job_completed"):
        worker.finalize_job("job_abc")

    saved = json.loads(worker.redis_client.set.call_args_list[-1][0][1])
    assert "micro_inferences" not in saved
    assert "embeddings" not in saved
