"""Dispatch ownership tests for the extraction fan-out (W2 2.1-2.2).

The inference dispatch must live in the entry-stage fan-out (extraction),
independent of the entities step, and it must keep the race fix: the
`inferences:remaining`/`:total` counters are written BEFORE the first chunk
message is published (otherwise consumers DECR a non-existent key -> -1).

These fakes keep ONE ordered event log (unlike golden_utils, which separates
Redis writes from published messages) so the counter-before-publish ordering
is directly assertable.
"""

import asyncio
import base64
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

for _mod in (
    "aio_pika",
    "aio_pika.abc",
    "aiohttp",
    "langdetect",
    "magic",
    "redis",
    "textstat",
    "tiktoken",
    "prometheus_client",
    "pypdfium2",
    "PIL",
    "PIL.Image",
):
    sys.modules.setdefault(_mod, MagicMock())

import worker  # noqa: E402
from fixtures import build_pdf  # noqa: E402
from golden_utils import FakeMsg, FakeStore  # noqa: E402
from pkg.events_python import EventBus  # noqa: E402
from pkg.worker_common.inference_dispatch import (  # noqa: E402
    INFERENCES_QUEUE,
    INFERENCES_TTL_SECONDS,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
JOB_ID = "dispatch-job"

# Longer than one char-fallback chunk (512 tokens * 4 chars), so the fan-out
# must publish more than one chunk message.
LONG_TEXT = "Escritura publica de compraventa entre las partes comparecientes. " * 300


def _metadata() -> dict:
    return {
        "filename": "document.pdf",
        "file_size_bytes": 10,
        "sha256": "0" * 64,
        "mime_type": "application/pdf",
        "page_count": 1,
        "exif_data": {},
        "author": None,
        "title": None,
        "subject": None,
        "creator": None,
        "producer": None,
        "creation_date": None,
        "modification_date": None,
        "encrypted": False,
    }


async def _fake_docling(self, document_bytes, filename, text=LONG_TEXT):
    return {
        "document": {
            "md_content": text,
            "text_content": None,
            "pages": [{"page_no": 1}],
        },
        "task_id": "dispatch",
        "task_status": "success",
    }


async def _fake_metadata(*args, **kwargs):
    return _metadata()


class _Recorder:
    def __init__(self):
        self.events = []

    def add(self, kind, *rest):
        self.events.append((kind,) + rest)


class _Redis:
    def __init__(self, recorder):
        self.rec = recorder
        self.data = {}

    def set(self, key, value):
        self.rec.add("redis:set", key, value)
        self.data[key] = value

    def setex(self, key, ttl, value):
        self.rec.add("redis:setex", key, ttl, value)
        self.data[key] = value

    def hset(self, key, *args, **kw):
        self.rec.add("redis:hset", key, args, kw)

    def hget(self, key, field):
        return None

    def get(self, key):
        return self.data.get(key)

    def publish(self, channel, msg):
        self.rec.add("redis:publish", channel)


class _Exchange:
    def __init__(self, recorder):
        self.rec = recorder

    async def publish(self, message, routing_key=None, **kw):
        self.rec.add("amqp:publish", routing_key, message.body)


class _Channel:
    def __init__(self, recorder):
        self.default_exchange = _Exchange(recorder)


class _Incoming:
    def __init__(self, body: bytes):
        self.body = body

    @staticmethod
    def _noop_cm():
        class _CM:
            async def __aenter__(self):
                return None

            async def __aexit__(self, *exc):
                return False

        return _CM()

    def process(self, requeue: bool = False):
        return self._noop_cm()


def _run(monkeypatch, *, profile: str, features=None, text: str = LONG_TEXT) -> _Recorder:
    monkeypatch.setenv(
        "PIPELINE_CONFIG_PATH", str(PROJECT_ROOT / "configs" / "pipeline.json")
    )
    monkeypatch.setattr(worker, "analyze_text", lambda t: {"language": "es"})
    monkeypatch.setattr(worker, "tokenizer", None)
    monkeypatch.setattr(worker, "extract_document_metadata", _fake_metadata)
    monkeypatch.setattr(worker, "STORE", FakeStore())
    monkeypatch.setattr(
        worker.ExtractionWorker,
        "_docling_convert_async",
        lambda self, document_bytes, filename: _fake_docling(
            self, document_bytes, filename, text=text
        ),
    )
    monkeypatch.setattr(
        worker.SourceClassifier,
        "classify",
        lambda text: {
            "document_type": "notariado",
            "confidence": 0.9,
            "classifier_version": "test",
        },
    )
    monkeypatch.setattr(worker.magic, "from_buffer", lambda *a, **k: "application/pdf")
    monkeypatch.setattr(worker.magic, "from_file", lambda *a, **k: "application/pdf")
    monkeypatch.setattr(
        worker,
        "aio_pika",
        SimpleNamespace(
            Message=FakeMsg,
            DeliveryMode=SimpleNamespace(PERSISTENT="PERSISTENT"),
        ),
    )

    recorder = _Recorder()
    instance = worker.ExtractionWorker()
    instance.redis_client = _Redis(recorder)
    instance.event_bus = EventBus(instance.redis_client)
    channel = _Channel(recorder)

    body = {
        "job_id": JOB_ID,
        "document_base64": base64.b64encode(build_pdf(["page one"])).decode("ascii"),
        "profile": profile,
    }
    if features is not None:
        body["features"] = features

    asyncio.run(
        instance._process_message_async(
            _Incoming(json.dumps(body).encode("utf-8")), channel
        )
    )
    return recorder


def _body(event):
    payload = event[2]
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8")
    try:
        return json.loads(payload)
    except (TypeError, ValueError):
        return None


def _counter_index(events) -> int:
    for index, event in enumerate(events):
        if event[0] in ("redis:set", "redis:setex") and event[1].endswith(
            ":inferences:remaining"
        ):
            return index
    return None


def _counter_value(events, suffix: str):
    for event in events:
        if event[0] in ("redis:set", "redis:setex") and event[1].endswith(suffix):
            return int(event[-1])
    return None


def _inference_messages(events) -> list:
    """Chunk messages published to the inferences queue (job envelopes excluded)."""
    messages = []
    for event in events:
        if event[0] != "amqp:publish" or event[1] != INFERENCES_QUEUE:
            continue
        payload = _body(event)
        if isinstance(payload, dict) and payload.get("chunk_text"):
            messages.append(payload)
    return messages


def _job_envelope_queues(events) -> list:
    queues = []
    for event in events:
        if event[0] != "amqp:publish":
            continue
        payload = _body(event)
        if isinstance(payload, dict) and "document_metadata" in payload:
            queues.append(event[1])
    return queues


def test_counters_are_set_before_publishing_chunks(monkeypatch):
    recorder = _run(monkeypatch, profile="full", features=[])
    events = recorder.events

    counter_index = _counter_index(events)
    messages = _inference_messages(events)
    first_message_index = next(
        index
        for index, event in enumerate(events)
        if event[0] == "amqp:publish"
        and event[1] == INFERENCES_QUEUE
        and (_body(event) or {}).get("chunk_text")
    )

    assert counter_index is not None, "inferences:remaining counter was never set"
    assert first_message_index > counter_index, (
        "counters must be written BEFORE the first chunk publish (race fix)"
    )
    assert _counter_value(events, ":inferences:total") == len(messages)

    counter_event = events[counter_index]
    assert counter_event[0] == "redis:setex"
    assert counter_event[2] == INFERENCES_TTL_SECONDS
    assert _counter_value(events, ":inferences:remaining") == len(messages)


def test_publishes_one_message_per_non_empty_chunk(monkeypatch):
    recorder = _run(monkeypatch, profile="full", features=[])
    messages = _inference_messages(recorder.events)

    assert len(messages) >= 2, "expected the long fixture text to yield >1 chunk"
    total = _counter_value(recorder.events, ":inferences:total")
    assert total == len(messages)

    chunk_ids = set()
    for message in messages:
        assert message["job_id"] == JOB_ID
        assert message["chunk_text"].strip()
        assert message["total_chunks"] == total
        assert message["source_type"] == "notariado"
        assert message["entities"] == []
        chunk_ids.add(message["chunk_id"])
    assert len(chunk_ids) == len(messages)


def test_rag_profile_dispatches_without_entities_step(monkeypatch):
    recorder = _run(monkeypatch, profile="rag", features=[])

    assert "entities" not in _job_envelope_queues(recorder.events)
    assert _counter_index(recorder.events) is not None
    assert len(_inference_messages(recorder.events)) >= 2


def _step_events(events, step: str) -> list:
    """hset writes on the job steps hash for a given step."""
    return [
        event
        for event in events
        if event[0] == "redis:hset"
        and event[1] == f"orchestrator:job:{JOB_ID}:steps"
        and step in event[2]
    ]


def test_no_chunks_completes_inferences_step_without_routing(monkeypatch):
    """F1: nothing to infer must not hang the job waiting for the step.

    Whitespace-only text passes the extraction guard (`if not text`) but yields
    no chunk from `chunk_text`, so no counters are written and no chunk message
    exists. The step must be completed as a no-op and the `inferences` job
    envelope must NOT be published.
    """
    recorder = _run(monkeypatch, profile="full", features=[], text="   \n  \t ")
    events = recorder.events

    assert _counter_index(events) is None, "no counters without chunks"
    assert _inference_messages(events) == []
    assert INFERENCES_QUEUE not in _job_envelope_queues(events), (
        "no inferences envelope: the step is already completed"
    )
    step_events = _step_events(events, "inferences")
    assert step_events, "inferences step must be marked completed"
    assert "completed" in step_events[0][2]
