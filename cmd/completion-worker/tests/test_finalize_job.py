"""Unit tests for the new finalize_job() output structure."""
import json
import sys
import os
import time
import msgpack
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))

from worker import CompletionWorker


def _get_results(worker):
    """Extract the results JSON from the redis_client.set call with ':results' key."""
    results_call = next(
        c for c in worker.redis_client.set.call_args_list
        if c[0][0].endswith(":results")
    )
    return json.loads(results_call[0][1])


def _make_redis_pipeline(chunks, entities_raw, micro_inferences=None):
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


def _make_worker():
    with patch("redis.from_url"):
        w = CompletionWorker()
        w.redis_client = MagicMock()
        w.redis_raw = MagicMock()
        return w


def test_embeddings_nested_inside_chunks():
    """Embeddings must be inside each chunk, not at top level."""
    worker = _make_worker()
    chunks = [{"chunk_id": "chunk_000", "text": "hello"}]
    embeddings_dict = {"chunk_000": [0.1, 0.2, 0.3]}

    pipe = MagicMock()
    pipe.execute.return_value = _make_redis_pipeline(chunks, [])
    worker.redis_client.pipeline.return_value = pipe
    worker.redis_raw.get.return_value = msgpack.packb(embeddings_dict, use_bin_type=True)
    worker.redis_client.set = MagicMock()
    worker.redis_client.hset = MagicMock()

    with patch.object(worker, "save_results_to_file"), patch.object(worker, "send_webhook"), \
         patch.object(worker.event_bus, "publish_job_completed"):
        worker.finalize_job("job_abc")

    saved = _get_results(worker)
    assert "embeddings" not in saved, "Top-level 'embeddings' key must be removed"
    assert saved["chunks"][0]["embeddings"] == [0.1, 0.2, 0.3]


def test_entities_dict_with_ids():
    """Top-level entities must be a dict keyed by entity_id."""
    worker = _make_worker()
    chunks = [{"chunk_id": "chunk_000", "text": "hello"}]
    entities_raw = [
        {"label": "PER", "text": "María", "confidence": 0.9, "chunk_id": "chunk_000", "entity_id": "abc000000001"},
        {"label": "PER", "text": "María", "confidence": 0.8, "chunk_id": "chunk_000", "entity_id": "abc000000001"},
    ]

    pipe = MagicMock()
    pipe.execute.return_value = _make_redis_pipeline(chunks, entities_raw)
    worker.redis_client.pipeline.return_value = pipe
    worker.redis_raw.get.return_value = None
    worker.redis_client.set = MagicMock()
    worker.redis_client.hset = MagicMock()

    with patch.object(worker, "save_results_to_file"), patch.object(worker, "send_webhook"), \
         patch.object(worker.event_bus, "publish_job_completed"):
        worker.finalize_job("job_abc")

    saved = _get_results(worker)
    assert isinstance(saved["entities"], dict), "entities must be a dict"
    assert "abc000000001" in saved["entities"]
    assert saved["entities"]["abc000000001"]["confidence"] == 0.9  # highest wins
    assert len(saved["entities"]) == 1  # deduplicated


def test_inferences_nested_in_chunks():
    """Inferences must appear inside each chunk, not at top level."""
    worker = _make_worker()
    chunks = [{"chunk_id": "chunk_000", "text": "hello"}]
    micro_inferences = [
        {"chunk_id": "chunk_000", "inferences": [
            {"text": "fact one", "confidence": 0.9, "entity_refs": ["María"]}
        ]}
    ]

    pipe = MagicMock()
    pipe.execute.return_value = _make_redis_pipeline(chunks, [], micro_inferences)
    worker.redis_client.pipeline.return_value = pipe
    worker.redis_raw.get.return_value = None
    worker.redis_client.set = MagicMock()
    worker.redis_client.hset = MagicMock()

    with patch.object(worker, "save_results_to_file"), patch.object(worker, "send_webhook"), \
         patch.object(worker.event_bus, "publish_job_completed"):
        worker.finalize_job("job_abc")

    saved = _get_results(worker)
    assert "micro_inferences" not in saved, "Top-level micro_inferences must be removed"
    chunk = saved["chunks"][0]
    assert chunk["inferences"][0]["text"] == "fact one"
    assert chunk["inferences"][0]["entity_refs"] == ["María"]


def test_entity_ids_in_chunks():
    """Each chunk must have entity_ids list referencing global entity dict."""
    worker = _make_worker()
    chunks = [{"chunk_id": "chunk_000", "text": "hello"}]
    entities_raw = [
        {"label": "PER", "text": "María", "confidence": 0.9, "chunk_id": "chunk_000", "entity_id": "abc000000001"},
    ]

    pipe = MagicMock()
    pipe.execute.return_value = _make_redis_pipeline(chunks, entities_raw)
    worker.redis_client.pipeline.return_value = pipe
    worker.redis_raw.get.return_value = None
    worker.redis_client.set = MagicMock()
    worker.redis_client.hset = MagicMock()

    with patch.object(worker, "save_results_to_file"), patch.object(worker, "send_webhook"), \
         patch.object(worker.event_bus, "publish_job_completed"):
        worker.finalize_job("job_abc")

    saved = _get_results(worker)
    chunk = saved["chunks"][0]
    assert "entity_ids" in chunk
    assert "abc000000001" in chunk["entity_ids"]


def test_no_top_level_embeddings_or_micro_inferences():
    """Top-level 'embeddings' and 'micro_inferences' keys must be absent."""
    worker = _make_worker()
    chunks = [{"chunk_id": "chunk_000", "text": "hello"}]
    pipe = MagicMock()
    pipe.execute.return_value = _make_redis_pipeline(chunks, [])
    worker.redis_client.pipeline.return_value = pipe
    worker.redis_raw.get.return_value = None
    worker.redis_client.set = MagicMock()
    worker.redis_client.hset = MagicMock()

    with patch.object(worker, "save_results_to_file"), patch.object(worker, "send_webhook"), \
         patch.object(worker.event_bus, "publish_job_completed"):
        worker.finalize_job("job_abc")

    saved = _get_results(worker)
    assert "micro_inferences" not in saved
    assert "embeddings" not in saved


def test_deduplicate_entities_fallback_without_entity_id():
    """deduplicate_entities() must generate entity_id for entities missing the field."""
    worker = _make_worker()
    entities = [
        {"label": "ORG", "text": "ACME", "confidence": 0.8},  # no entity_id
        {"label": "ORG", "text": "ACME", "confidence": 0.9},  # no entity_id, same key
    ]
    result = worker.deduplicate_entities(entities)
    assert len(result) == 1
    eid = list(result.keys())[0]
    assert len(eid) == 12
    assert result[eid]["confidence"] == 0.9  # highest wins
