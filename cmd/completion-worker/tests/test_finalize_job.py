"""Unit tests for the new finalize_job() output structure."""
import json
import time
import msgpack
from unittest.mock import MagicMock, patch

from completion_worker import CompletionWorker
from pkg.worker_common.entity_utils import deduplicate_entities


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
    with patch("redis.from_url"), \
         patch("pkg.worker_common.pubsub_base.Counter", return_value=MagicMock()), \
         patch("pkg.worker_common.pubsub_base.Histogram", return_value=MagicMock()):
        w = CompletionWorker()
        w._redis_client = MagicMock()
        w._redis_raw = MagicMock()
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

    with patch.object(worker, "save_results_to_file") as mock_save, \
         patch.object(worker, "send_webhook"), \
         patch.object(worker.event_bus, "publish_job_completed"):
        worker.finalize_job("job_abc")
        saved = mock_save.call_args[0][1]

    assert "embeddings" not in saved, "Top-level 'embeddings' key must be removed"
    assert saved["chunks"][0]["embeddings"] == [0.1, 0.2, 0.3]


def test_finalize_text_resolves_artifact_ref(monkeypatch, tmp_path):
    """finalize_job() resolves a sha256 ref in :text via the artifact store."""
    import completion_worker as cw
    from pkg.worker_common.artifact_store import FSStore

    store = FSStore(str(tmp_path))
    ref = store.put("documento de prueba".encode("utf-8"))
    monkeypatch.setattr(cw, "STORE", store)

    worker = _make_worker()
    chunks = [{"chunk_id": "chunk_000", "text": "hello"}]

    pipe = MagicMock()
    result = list(_make_redis_pipeline(chunks, []))
    result[2] = ref  # replace raw text slot with an artifact ref
    pipe.execute.return_value = tuple(result)
    worker.redis_client.pipeline.return_value = pipe
    worker.redis_raw.get.return_value = None
    worker.redis_client.set = MagicMock()
    worker.redis_client.hset = MagicMock()

    with patch.object(worker, "save_results_to_file") as mock_save, \
         patch.object(worker, "send_webhook"), \
         patch.object(worker.event_bus, "publish_job_completed"):
        worker.finalize_job("job_abc")
        saved = mock_save.call_args[0][1]

    assert saved["text"] == "documento de prueba"


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

    with patch.object(worker, "save_results_to_file") as mock_save, \
         patch.object(worker, "send_webhook"), \
         patch.object(worker.event_bus, "publish_job_completed"):
        worker.finalize_job("job_abc")
        saved = mock_save.call_args[0][1]

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

    with patch.object(worker, "save_results_to_file") as mock_save, \
         patch.object(worker, "send_webhook"), \
         patch.object(worker.event_bus, "publish_job_completed"):
        worker.finalize_job("job_abc")
        saved = mock_save.call_args[0][1]

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

    with patch.object(worker, "save_results_to_file") as mock_save, \
         patch.object(worker, "send_webhook"), \
         patch.object(worker.event_bus, "publish_job_completed"):
        worker.finalize_job("job_abc")
        saved = mock_save.call_args[0][1]

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

    with patch.object(worker, "save_results_to_file") as mock_save, \
         patch.object(worker, "send_webhook"), \
         patch.object(worker.event_bus, "publish_job_completed"):
        worker.finalize_job("job_abc")
        saved = mock_save.call_args[0][1]

    assert "micro_inferences" not in saved
    assert "embeddings" not in saved


def test_deduplicate_entities_fallback_without_entity_id():
    """deduplicate_entities() must generate entity_id for entities missing the field."""
    entities = [
        {"label": "ORG", "text": "ACME", "confidence": 0.8},  # no entity_id
        {"label": "ORG", "text": "ACME", "confidence": 0.9},  # no entity_id, same key
    ]
    result = deduplicate_entities(entities)
    assert len(result) == 1
    eid = list(result.keys())[0]
    assert len(eid) == 12
    assert result[eid]["confidence"] == 0.9  # highest wins


# ---------------------------------------------------------------------------
# Fuzzy deduplication tests
# ---------------------------------------------------------------------------

def test_deduplicate_entities_fuzzy_identical_text():
    """Entities with identical text and same label must merge into one."""
    entities = [
        {"label": "PER", "text": "María García", "confidence": 0.9, "entity_id": "aaa000000001"},
        {"label": "PER", "text": "María García", "confidence": 0.7, "entity_id": "aaa000000002"},
    ]
    result = deduplicate_entities(entities)
    assert len(result) == 1, "identical text → must merge"
    eid = list(result.keys())[0]
    assert result[eid]["confidence"] == 0.9  # highest wins


def test_deduplicate_entities_fuzzy_similar_text():
    """Entities with very similar text (>= threshold) and same label must merge."""
    # "Departamento de Educacion" vs "Departamento de Educación" — differ only in accent
    entities = [
        {"label": "ORG", "text": "Departamento de Educacion", "confidence": 0.8, "entity_id": "bbb000000001"},
        {"label": "ORG", "text": "Departamento de Educación", "confidence": 0.9, "entity_id": "bbb000000002"},
    ]
    result = deduplicate_entities(entities)
    assert len(result) == 1, "accent-only difference → must merge after normalization"
    eid = list(result.keys())[0]
    assert result[eid]["confidence"] == 0.9  # highest wins


def test_deduplicate_entities_fuzzy_different_text():
    """Entities with clearly different text must remain separate."""
    entities = [
        {"label": "PER", "text": "María García", "confidence": 0.9, "entity_id": "ccc000000001"},
        {"label": "PER", "text": "Juan López",   "confidence": 0.8, "entity_id": "ccc000000002"},
    ]
    result = deduplicate_entities(entities)
    assert len(result) == 2, "clearly different texts → must stay separate"


def test_deduplicate_entities_fuzzy_different_label_no_merge():
    """Same text but different label must NOT merge."""
    entities = [
        {"label": "PER", "text": "Aragón", "confidence": 0.9, "entity_id": "ddd000000001"},
        {"label": "LOC", "text": "Aragón", "confidence": 0.8, "entity_id": "ddd000000002"},
    ]
    result = deduplicate_entities(entities)
    assert len(result) == 2, "same text but different label → must NOT merge"


def test_entity_offsets_preserved():
    """Offsets from highest confidence entity must be preserved after deduplication."""
    entities = [
        {
            "label": "PER",
            "text": "María",
            "confidence": 0.7,
            "entity_id": "eee000000001",
            "start": 10,
            "end": 15,
            "chunk_id": "chunk_001",
        },
        {
            "label": "PER",
            "text": "María",
            "confidence": 0.9,
            "entity_id": "eee000000002",
            "start": 100,
            "end": 105,
            "chunk_id": "chunk_002",
        },
    ]
    result = deduplicate_entities(entities)
    assert len(result) == 1
    eid = list(result.keys())[0]
    assert result[eid]["start_offset"] == 100
    assert result[eid]["end_offset"] == 105
    assert result[eid]["chunk_id"] == "chunk_002"
