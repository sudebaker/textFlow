"""Unit tests for extract_regex_parallel and regex settings wiring."""

import json
import time
from unittest.mock import MagicMock

import app.config.settings as settings_module
import entities_worker as ew


def test_settings_regex_service_url_falls_back_to_old_env_var(monkeypatch):
    monkeypatch.delenv("REGEX_SERVICE_URL", raising=False)
    monkeypatch.setenv("REGEX_ENTITY_EXTRACTOR_URL", "http://regex-custom:9999")

    settings = settings_module.Settings()

    assert settings.regex_service_url == "http://regex-custom:9999"


def test_settings_regex_service_url_prefers_new_env_var(monkeypatch):
    monkeypatch.setenv("REGEX_SERVICE_URL", "http://regex-new:8082")
    monkeypatch.setenv("REGEX_ENTITY_EXTRACTOR_URL", "http://regex-old:9999")

    settings = settings_module.Settings()

    assert settings.regex_service_url == "http://regex-new:8082"


def test_merges_regex_and_gliner_results():
    gliner = lambda: [{"label": "PER", "text": "Juan"}]
    regex = lambda text: [{"label": "LOC", "text": "Madrid"}]

    result = ew.extract_regex_parallel("Hola", regex, gliner)

    assert len(result) == 2


def test_runs_concurrently():
    def gliner():
        time.sleep(0.2)
        return ["g"]

    def regex(text):
        time.sleep(0.2)
        return ["r"]

    start = time.time()
    result = ew.extract_regex_parallel("Hola", regex, gliner)
    elapsed = time.time() - start

    assert result == ["g", "r"]
    assert elapsed < 0.35  # paralelo (~0.2s), no serial (~0.4s)


def test_degrades_silently_when_regex_raises():
    def regex(text):
        raise RuntimeError("boom")

    gliner = lambda: ["g"]

    result = ew.extract_regex_parallel("Hola", regex, gliner)

    assert result == ["g"]


def test_skips_regex_when_no_text():
    gliner = lambda: ["g"]
    regex = MagicMock(side_effect=AssertionError("must not be called"))

    result = ew.extract_regex_parallel("", regex, gliner)

    assert result == ["g"]
    regex.assert_not_called()


def test_skips_regex_when_regex_fn_none():
    gliner = lambda: ["g"]

    result = ew.extract_regex_parallel("Hola", None, gliner)

    assert result == ["g"]


def _build_worker():
    """Build an EntitiesWorker without BaseWorker.__init__ (avoids Prometheus
    registry collisions and real Redis/RabbitMQ connections)."""
    worker = ew.EntitiesWorker.__new__(ew.EntitiesWorker)
    worker.logger = MagicMock()
    worker.default_entities = ["PER", "ORG", "LOC"]
    worker.regex_enabled = True
    worker.regex_service_url = "http://regex-entity-extractor:8081"
    worker.regex_timeout = 30
    worker.model = MagicMock()
    worker.model.predict_entities.return_value = []
    worker._redis_client = MagicMock()
    worker._event_bus = MagicMock()
    worker.jobs_total = MagicMock()
    worker._publish_to_queue = MagicMock()
    return worker


def test_process_message_merges_regex_into_entities_raw():
    worker = _build_worker()
    worker.redis_client.get.side_effect = lambda key: (
        json.dumps("Juan trabaja en Madrid") if key.endswith(":text") else None
    )
    worker._extract_regex_entities = lambda text: [
        {
            "text": "Madrid",
            "label": "LOC",
            "confidence": 1.0,
            "start": 0,
            "end": 0,
            "chunk_id": "c1",
        }
    ]
    message = {
        "job_id": "job-1",
        "chunks": [
            {"chunk_id": "c1", "text": "Juan trabaja en Madrid", "start_offset": 0}
        ],
    }

    result = worker.process_message(message)

    assert result["status"] == "success"
    writes = {c[0][0]: c[0][1] for c in worker.redis_client.set.call_args_list}
    entities_raw = json.loads(writes["orchestrator:job:job-1:entities_raw"])
    assert any(e["label"] == "LOC" and e["text"] == "Madrid" for e in entities_raw)
    assert all("entity_id" in e for e in entities_raw)


def test_process_message_skips_regex_when_disabled():
    worker = _build_worker()
    worker.regex_enabled = False
    worker.redis_client.get.side_effect = lambda key: (
        json.dumps("Juan trabaja en Madrid") if key.endswith(":text") else None
    )
    worker._extract_regex_entities = lambda text: [
        {
            "text": "Madrid",
            "label": "LOC",
            "confidence": 1.0,
            "start": 0,
            "end": 0,
            "chunk_id": "c1",
        }
    ]
    message = {
        "job_id": "job-2",
        "chunks": [
            {"chunk_id": "c1", "text": "Juan trabaja en Madrid", "start_offset": 0}
        ],
    }

    worker.process_message(message)

    writes = {c[0][0]: c[0][1] for c in worker.redis_client.set.call_args_list}
    entities_raw = json.loads(writes["orchestrator:job:job-2:entities_raw"])
    assert entities_raw == []  # sin regex: solo GLiNER (mock devuelve [])
