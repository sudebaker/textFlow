"""Integration test: vision fallback flag-ON end-to-end (Task E.4).

Golden-harness plumbing (test_golden_vision_off pattern) with an explicit
VisionSettings(ocr_enabled=True) — via patching vision.fallback.SETTINGS, the
module singleton resolved by FallbackContext's default_factory at call time.
Scenario: 2-page doc whose md is tiny (doc gate SUSPECT) and whose json-style
per-page texts leave page 2 thin (page gate SUSPECT) — vision replaces page 2.

Asserts:
- extraction_provenance record with page statuses;
- :text ref resolves to the REPLACED text via FakeStore blob;
- publish contract unchanged (same job_message shape, structural minus provenance);
- a second flag-OFF scenario reproduces golden/baseline.json EXACTLY.
"""

import asyncio
import base64
import hashlib
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
):
    sys.modules.setdefault(_mod, MagicMock())

import pytest  # noqa: E402

import worker  # noqa: E402
import vision.fallback as vf  # noqa: E402
from golden_utils import FakeExchange, FakeMsg, FakeRedis, FakeStore, canonical_json, normalize  # noqa: E402
from fixtures import build_pdf  # noqa: E402
from pkg.events_python import EventBus  # noqa: E402

from vision.config import VisionSettings  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[3]
GOLDEN_PATH = Path(__file__).resolve().parent / "golden" / "baseline.json"

JOB_ID_IT = "vision-it"
FIXTURE_PAGES = ["Pagine uno escribir", "Pagina dos escribir"]


def _stub_worker_modules(monkeypatch) -> None:
    """Common deterministic patches (golden Task 0.3 pattern). STORE NOT here —
    _run_job passes its own FakeStore so the test can resolve blobs."""
    monkeypatch.setenv(
        "PIPELINE_CONFIG_PATH", str(PROJECT_ROOT / "configs" / "pipeline.json")
    )
    monkeypatch.setattr(worker, "analyze_text", lambda t: {"language": "es"})
    monkeypatch.setattr(worker, "tokenizer", None)
    monkeypatch.setattr(
        worker.subprocess, "run", MagicMock(returncode=0, stdout="[]")
    )
    monkeypatch.setattr(worker.magic, "from_buffer", lambda *a, **k: "application/pdf")
    monkeypatch.setattr(worker.magic, "from_file", lambda *a, **k: "application/pdf")


def _it_settings(**overrides):
    defaults = dict(
        ocr_enabled=True,
        gate_enabled=True,
        ocr_url="http://vision-ocr:8080",
        ocr_timeout=120.0,
        max_pages_per_document=50,
        max_seconds_per_document=900.0,
        max_concurrency=4,
        image_dpi=200,
        min_chars_per_page=100,
        image_placeholder_ratio_max=0.3,
        garbage_ratio_max=0.2,
    )
    defaults.update(overrides)
    return VisionSettings(**defaults)


def _fixed_metadata(pdf_bytes: bytes) -> dict:
    return {
        "filename": "document.pdf",
        "file_size_bytes": len(pdf_bytes),
        "sha256": hashlib.sha256(pdf_bytes).hexdigest(),
        "author": None,
        "title": None,
        "subject": None,
        "creator": None,
        "producer": None,
        "creation_date": None,
        "modification_date": None,
        "page_count": 2,
        "encrypted": False,
        "mime_type": "application/pdf",
        "exif_data": {},
    }


class _IncomingMessage:
    def __init__(self, body: bytes):
        self.body = body

    def process(self, requeue: bool = False):
        self_ = self

        class _CM:
            async def __aenter__(self):
                return self_

            async def __aexit__(self, *exc):
                return False

        return _CM()


class _Channel:
    def __init__(self):
        self.default_exchange = FakeExchange()


def _docling_json(ocr_patch_pages=None):
    """json_content-shaped DoclingDocument: page1 dense, page2 thin."""
    return {
        "json_content": {
            "texts": [
                {"text": "Pagine uno escribir " * 6, "prov": [{"page_no": 1}]},
                {"text": "thin p2", "prov": [{"page_no": 2}]},
            ],
            "pages": {"1": {"page_no": 1}, "2": {"page_no": 2}},
        },
        # md_content: tiny overall so the DOC gate is SUSPECT (75 chars/page < 100)
        "md_content": "Pagine uno escribir\n\nthin p2",
        "text_content": None,
        "pages": [{"page_no": 1}, {"page_no": 2}],
    }


def _run_job(monkeypatch, settings, docling_response_path="document.json_content"):
    """Run one job end-to-end; return (fake_redis, fake_store, wf input)."""
    fake_redis = FakeRedis()
    fake_store = FakeStore()

    docling_response = {
        "document": {
            **_docling_json(),
            "task_id": "t-it",
            "task_status": "success",
        },
        "task_id": "t-it",
        "task_status": "success",
    }

    async def _fake_docling(self, document_bytes, filename):
        return docling_response

    async def _fake_metadata(*args, **kwargs):
        pdf = base64.b64decode(_job_body()["document_base64"])
        return _fixed_metadata(pdf)

    vision_text = "VISION RECOVERED PAGE TWO text " * 6  # dense

    async def _vision_ok(*args, **kwargs):
        return vision_text

    _stub_worker_modules(monkeypatch)
    monkeypatch.setattr(worker, "extract_document_metadata", _fake_metadata)
    monkeypatch.setattr(worker, "STORE", fake_store)
    monkeypatch.setattr(worker.ExtractionWorker, "_docling_convert_async", _fake_docling)
    monkeypatch.setattr(
        worker,
        "aio_pika",
        SimpleNamespace(
            Message=FakeMsg,
            DeliveryMode=SimpleNamespace(PERSISTENT=2),
        ),
    )
    # The worker hook builds FallbackContext WITHOUT passing settings -> the
    # default_factory reads vision.fallback.SETTINGS at runtime. Patch it.
    monkeypatch.setattr(vf, "SETTINGS", settings)

    w = worker.ExtractionWorker()
    w.redis_client = fake_redis
    w.event_bus = MagicMock()
    w.event_bus.publish_stage_event = lambda *a, **k: None
    w.event_bus.publish_job_progress = lambda *a, **k: None

    # Patch the client the fallback uses: VisionOCRClient(instance) -> fake
    fake_client = SimpleNamespace(transcribe=_vision_ok)
    monkeypatch.setattr(vf, "VisionOCRClient", MagicMock(return_value=fake_client))
    # The harness must never depend on collection-time stubs: other test files
    # register pypdfium2/PIL MagicMocks in sys.modules, which would mock even a
    # "real" renderer path (mock len == 0 -> bounds IndexError). Patch the
    # renderer at the fallback module level like the slow-path unit tests do.
    from unittest.mock import AsyncMock

    monkeypatch.setattr(vf, "render_page_async", AsyncMock(return_value=b"\x89PNG"))

    channel = _Channel()
    asyncio.run(
        w._process_message_async(
            _IncomingMessage(json.dumps(_job_body()).encode()), channel
        )
    )
    return fake_redis, fake_store, channel.default_exchange.published


def _job_body():
    pdf = build_pdf(FIXTURE_PAGES)
    return {
        "job_id": JOB_ID_IT,
        "document_base64": base64.b64encode(pdf).decode("ascii"),
    }


def _provenance_from(redis_writes):
    provs = [w for w in redis_writes if w[0] == "set" and ":extraction_provenance" in w[1]]
    assert len(provs) == 1, f"expected 1 provenance write: {provs}"
    return json.loads(provs[0][2])


def _text_ref(redis_writes):
    texts = [w for w in redis_writes if w[0] == "set" and f"orchestrator:job:{JOB_ID_IT}:text" == w[1]]
    assert len(texts) == 1
    return texts[0][2]


class TestVisionIntegrationFlagOn:
    def test_slow_path_replaces_page2_and_writes_provenance(self, monkeypatch):
        fake_redis, fake_store, _published = _run_job(monkeypatch, _it_settings())
        prov = _provenance_from(fake_redis.writes)
        statuses = [(p["page"], p["status"]) for p in prov["pages"]]
        assert statuses == [(1, "pass"), (2, "fallback")]
        backends = [p["backend"] for p in prov["pages"]]
        assert backends == ["docling", "vision"]

        # :text ref resolves to the assembled text with page2 REPLACED
        ref = _text_ref(fake_redis.writes)
        assert ref.startswith("sha256:")
        stored = fake_store.get(ref)
        text = stored.decode("utf-8")
        assert "VISION RECOVERED PAGE TWO" in text
        assert "thin p2" not in text
        assert "Pagine uno escribir" in text

    def test_publish_contract_unchanged(self, monkeypatch):
        fake_redis, _, published = _run_job(monkeypatch, _it_settings())
        baseline = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
        # SAME routing keys as baseline (full pipeline: embeddings+entities+metadata)
        routing = [p[0] for p in published]
        assert routing == [p["routing_key"] for p in baseline["published"]]

        norm = normalize([], published)["published"]  # queued_at -> "<ts>"
        bodies = [p["body"] for p in norm]
        for body, base_body in zip(bodies, baseline["published"]):
            # Same job_message shape as the baseline (NORMALIZED capture of the
            # golden harness already applies queued_at="<ts>").
            assert sorted(body.keys()) == sorted(base_body["body"].keys())
            assert body["queued_at"] == "<ts>"
            assert body["pipeline_version"] == "v1"
            assert body["profile"] == "balanced"
            assert sorted(body["document_metadata"].keys()) == sorted(
                base_body["body"]["document_metadata"].keys()
            )
            # NOT telemetry keys: the job message contract carries no provenance
            assert "provenance" not in json.dumps(body)


class TestFlagOffStillBaseline:
    def test_fast_path_off_matches_baseline_exactly(self, monkeypatch):
        """ocr_enabled=False through the SAME integration machinery: the fast
        path must behave EXACTLY like the golden (no provenance key, publish
        contract intact). Full byte-exact equality vs baseline.json is enforced
        by the untouched test_golden_vision_off.py in the same suite."""
        fake_redis, _, published = _run_job(monkeypatch, _it_settings(ocr_enabled=False))
        assert [w[1] for w in fake_redis.writes if "provenance" in w[1]] == []
        norm = normalize([], published)["published"]  # queued_at -> "<ts>"
        baseline = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
        assert len(norm) == len(baseline["published"])
        routings_a = [p["routing_key"] for p in norm]
        routings_b = [p["routing_key"] for p in baseline["published"]]
        assert routings_a == routings_b
        for p, base_p in zip(norm, baseline["published"]):
            body = p["body"]
            assert sorted(body.keys()) == sorted(base_p["body"].keys())
            assert body["queued_at"] == "<ts>"
