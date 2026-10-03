"""Tests for vision.fallback + vision.metrics: log-only quality gate (spec §8, Phase C).

run_quality_flow() is log+metrics ONLY in this phase: it must return ctx.text
EXACTLY (byte-identical), never touch Redis, never call get_document_bytes
(fast path = no rendering, no OCR call, no provenance writes).
"""

import base64
import json
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

# Same stub list as test_metadata.py:16-27 — the full third-party import
# surface of worker.py is not installed in the air-gapped test env.
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

import asyncio  # noqa: E402

import pytest  # noqa: E402

import worker  # noqa: E402
from fixtures import build_pdf  # noqa: E402
from golden_utils import FakeExchange, FakeRedis, FakeStore  # noqa: E402
from vision.config import VisionSettings  # noqa: E402

from pathlib import Path  # noqa: E402

PROJECT_ROOT = Path(
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
)
JOB_ID = "gate-test"

DENSE_TEXT = (
    "El contrato de arrendamiento se firma en Madrid el tres de marzo. "
    "El inquilino se compromete al pago mensual de la renta acordada. "
    "Cualquier modificación deberá notificarse por escrito con treinta días "
    "de antelación, conforme a lo dispuesto en la cláusula quinta. "
    "En caso de incumplimiento, el arrendador podrá iniciar procedimientos. "
) * 3


def make_settings(**overrides):
    defaults = dict(
        ocr_enabled=False,
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


# --- vision.metrics (Task C.2) ----------------------------------------------


class TestMetricsModule:
    def test_metric_names_and_labels(self):
        # This test requires the REAL prometheus_client: purge the MagicMock
        # stubs (sys.modules.setdefault at module top) and re-import fresh.
        # vision.metrics is purged too so it re-imports against the real
        # module (a reload would double-register the timeseries).
        import importlib

        for name in list(sys.modules):
            if (
                name == "prometheus_client"
                or name.startswith("prometheus_client.")
                or name == "vision.metrics"
            ):
                del sys.modules[name]
        try:
            importlib.import_module("prometheus_client")
            import vision.metrics as m
        except ImportError:
            pytest.skip("prometheus_client not available (air-gapped env)")

        assert m.quality_gate_documents_total._name == "extraction_worker_quality_gate_documents"  # _total is a sample suffix
        assert tuple(m.quality_gate_documents_total._labelnames) == ("decision",)
        assert m.vision_documents_total._name == "extraction_worker_vision_documents"
        assert tuple(m.vision_documents_total._labelnames) == ("path",)
        assert m.vision_pages_total._name == "extraction_worker_vision_pages"
        assert tuple(m.vision_pages_total._labelnames) == ("backend",)
        assert m.vision_page_seconds._name == "extraction_worker_vision_page_seconds"
        assert m.vision_document_seconds._name == "extraction_worker_vision_document_seconds"


# --- run_quality_flow: log-only contract (Task C.3 skeleton) ------------------


def make_ctx(text=DENSE_TEXT, **overrides):
    from vision.fallback import FallbackContext

    defaults = dict(
        job_id=JOB_ID,
        text=text,
        docling_document={},
        page_count=None,
        get_document_bytes=lambda: b"%PDF-fake",
        redis_client=FakeRedis(),
        raise_if_cancelled=lambda job_id: None,
    )
    defaults.update(overrides)
    ctx = FallbackContext(**defaults)
    return ctx, defaults["get_document_bytes"], defaults["redis_client"]


class TestLogOnlyContract:
    def test_pass_returns_text_exact(self):
        from vision.fallback import run_quality_flow

        ctx, _, _ = make_ctx()
        result = asyncio.run(run_quality_flow(ctx))
        assert result == DENSE_TEXT
        assert result is ctx.text

    def test_suspect_returns_text_exact(self):
        from vision.fallback import run_quality_flow

        ctx, _, _ = make_ctx(text="too short")  # 9 chars over 2 pages\a -> suspect via default page_count=None
        result = asyncio.run(run_quality_flow(ctx))
        assert result == "too short"
        assert result.encode("utf-8") == "too short".encode("utf-8")

    def test_pass_and_suspect_are_byte_equal(self):
        from vision.fallback import run_quality_flow

        for text in (DENSE_TEXT, "short"):
            ctx, _, _ = make_ctx(text=text)
            out = asyncio.run(run_quality_flow(ctx))
            assert out.encode("utf-8") == text.encode("utf-8")

    def test_never_calls_get_document_bytes(self):
        from vision.fallback import run_quality_flow

        get_bytes = MagicMock()
        ctx, _, _ = make_ctx(text="short", get_document_bytes=get_bytes)
        asyncio.run(run_quality_flow(ctx))
        get_bytes.assert_not_called()

    def test_never_calls_get_document_bytes_on_pass(self):
        from vision.fallback import run_quality_flow

        get_bytes = MagicMock()
        ctx, _, _ = make_ctx(get_document_bytes=get_bytes)
        asyncio.run(run_quality_flow(ctx))
        get_bytes.assert_not_called()

    def test_never_writes_redis(self):
        from vision.fallback import run_quality_flow

        for text in (DENSE_TEXT, "short"):
            ctx, _, redis = make_ctx(text=text)
            asyncio.run(run_quality_flow(ctx))
            assert redis.writes == []

    def test_never_calls_raise_if_cancelled(self):
        from vision.fallback import run_quality_flow

        raise_if_cancelled = MagicMock()
        ctx, _, _ = make_ctx(raise_if_cancelled=raise_if_cancelled)
        asyncio.run(run_quality_flow(ctx))
        raise_if_cancelled.assert_not_called()

    def test_docling_document_carried_but_unused(self):
        from vision.fallback import run_quality_flow

        doc = {"texts": [{"text": "hola", "prov": [{"page_no": 1}]}]}
        ctx, _, _ = make_ctx(docling_document=doc)
        assert asyncio.run(run_quality_flow(ctx)) == DENSE_TEXT


class TestMetricsAndLogging:
    def test_pass_increments_fast_path_counter(self):
        import vision.fallback as fb

        run_quality_flow = fb.run_quality_flow

        # Patch the fallback MODULE attributes — patching Counter.labels is
        # unreliable here: the MagicMock prometheus_client stub returns the
        # SAME child object for every Counter() call, so both counters alias.
        gate = MagicMock()
        docs = MagicMock()
        with patch.object(fb, "quality_gate_documents_total", gate), patch.object(
            fb, "vision_documents_total", docs
        ):
            ctx, _, _ = make_ctx()
            asyncio.run(run_quality_flow(ctx))
        gate.labels.assert_called_once_with(decision="pass")
        gate.labels.return_value.inc.assert_called_once()
        docs.labels.assert_called_once_with(path="fast_path")
        docs.labels.return_value.inc.assert_called_once()

    def test_suspect_increments_gate_counter_no_fast_path(self):
        import vision.fallback as fb

        run_quality_flow = fb.run_quality_flow

        gate = MagicMock()
        docs = MagicMock()
        with patch.object(fb, "quality_gate_documents_total", gate), patch.object(
            fb, "vision_documents_total", docs
        ):
            ctx, _, _ = make_ctx(text="short")
            asyncio.run(run_quality_flow(ctx))
        gate.labels.assert_called_once_with(decision="suspect")
        gate.labels.return_value.inc.assert_called_once()
        docs.labels.assert_not_called()

    def test_pass_logs_decision_info(self, caplog):
        from vision.fallback import run_quality_flow

        ctx, _, _ = make_ctx()
        with caplog.at_level("INFO", logger="vision.fallback"):
            asyncio.run(run_quality_flow(ctx))
        assert any(
            "quality gate" in r.message and "job=gate-test" in r.message
            for r in caplog.records
        )

    def test_suspect_logs_decision_info(self, caplog):
        from vision.fallback import run_quality_flow

        ctx, _, _ = make_ctx(text="short")
        with caplog.at_level("INFO", logger="vision.fallback"):
            asyncio.run(run_quality_flow(ctx))
        assert any(
            "decision=suspect" in r.message and "job=gate-test" in r.message
            for r in caplog.records
        )


class TestFlagEarlyReturn:
    def test_both_flags_off_returns_text_before_counters(self):
        from vision.fallback import (
            quality_gate_documents_total,
            run_quality_flow,
            vision_documents_total,
        )

        gate = MagicMock()
        docs = MagicMock()
        settings = make_settings(gate_enabled=False, ocr_enabled=False)
        with patch.object(quality_gate_documents_total, "labels", gate), patch.object(
            vision_documents_total, "labels", docs
        ):
            ctx, _, _ = make_ctx(settings=settings)
            assert asyncio.run(run_quality_flow(ctx)) == DENSE_TEXT
        gate.assert_not_called()
        docs.assert_not_called()

    def test_both_flags_off_never_touches_redis_or_bytes(self):
        from vision.fallback import run_quality_flow

        get_bytes = MagicMock()
        settings = make_settings(gate_enabled=False, ocr_enabled=False)
        ctx, _, redis = make_ctx(get_document_bytes=get_bytes, settings=settings)
        assert asyncio.run(run_quality_flow(ctx)) == DENSE_TEXT
        get_bytes.assert_not_called()
        assert redis.writes == []

    def test_ocr_disabled_gate_enabled_still_observes(self):
        import vision.fallback as fb

        gate = MagicMock()
        settings = make_settings(ocr_enabled=False, gate_enabled=True)
        with patch.object(fb, "quality_gate_documents_total", gate):
            ctx, _, _ = make_ctx(settings=settings)
            assert asyncio.run(fb.run_quality_flow(ctx)) == DENSE_TEXT
        gate.labels.assert_called_once_with(decision="pass")


# --- worker hook integration (Task C.3 hook) ---------------------------------


class _Channel:
    def __init__(self):
        self.default_exchange = FakeExchange()


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


def _fixed_metadata(pdf_bytes=None):
    return {
        "filename": "doc.pdf",
        "file_size_bytes": len(pdf_bytes or b""),
        "sha256": "0" * 64,
        "mime_type": "application/pdf",
        "author": None,
        "title": None,
        "subject": None,
        "creator": None,
        "producer": None,
        "creation_date": None,
        "modification_date": None,
        "page_count": 1,
        "encrypted": False,
        "exif_data": {},
    }


def _integration_worker(monkeypatch, docling_text="Hello world page one"):
    """Build an ExtractionWorker wired to fakes with a stubbed docling call."""
    fake_redis = FakeRedis()

    async def _fake_docling(self, document_bytes, filename):
        return {"document": {"md_content": docling_text, "pages": {"1": {}}}}

    monkeypatch.setenv(
        "PIPELINE_CONFIG_PATH", str(PROJECT_ROOT / "configs" / "pipeline.json")
    )
    monkeypatch.setattr(
        worker, "analyze_text", lambda t: {"language": "es", "char_count": len(t)}
    )
    monkeypatch.setattr(worker, "tokenizer", None)

    async def _fake_metadata(*a, **k):
        return _fixed_metadata()

    monkeypatch.setattr(worker, "extract_document_metadata", _fake_metadata)
    monkeypatch.setattr(worker.subprocess, "run", MagicMock(returncode=0, stdout="[]"))
    monkeypatch.setattr(worker.magic, "from_buffer", lambda *a, **k: "application/pdf")
    monkeypatch.setattr(worker.magic, "from_file", lambda *a, **k: "application/pdf")
    monkeypatch.setattr(worker, "STORE", FakeStore())
    monkeypatch.setattr(worker.ExtractionWorker, "_docling_convert_async", _fake_docling)

    w = worker.ExtractionWorker()
    w.redis_client = fake_redis
    w.event_bus = MagicMock()
    return w, fake_redis


class TestWorkerHook:
    def test_hook_runs_and_logs_without_behavior_change(self, monkeypatch, caplog):
        pdf = build_pdf(["Hello world page one"])
        body = {
            "job_id": JOB_ID,
            "document_base64": base64.b64encode(pdf).decode("ascii"),
        }
        w, fake_redis = _integration_worker(monkeypatch)
        with caplog.at_level("INFO", logger="vision.fallback"):
            asyncio.run(
                w._process_message_async(
                    _IncomingMessage(json.dumps(body).encode()), _Channel()
                )
            )

        assert any("quality gate" in r.message for r in caplog.records)
        assert any("job=gate-test" in r.message for r in caplog.records)
        # Log-only: no vision artifacts written to Redis (provenance belongs
        # to the slow path, Phase E — never written in this phase).
        keys = [w[1] for w in fake_redis.writes if w[0] == "hset" or w[0] == "set"]
        assert not any("vision" in k or "provenance" in k for k in keys)

    def test_hook_suspect_document_still_written_normally(self, monkeypatch, caplog):
        # A suspect document (placeholder-only text) must NOT alter the write
        # contract: the job proceeds normally with docling's text untouched.
        pdf = build_pdf(["Hello world page one"])
        body = {
            "job_id": JOB_ID,
            "document_base64": base64.b64encode(pdf).decode("ascii"),
        }
        suspect_text = "![](img1.png)\n![](img2.png)"
        w, fake_redis = _integration_worker(monkeypatch, docling_text=suspect_text)
        with caplog.at_level("INFO", logger="vision.fallback"):
            asyncio.run(
                w._process_message_async(
                    _IncomingMessage(json.dumps(body).encode()), _Channel()
                )
            )
        assert any("decision=suspect" in r.message for r in caplog.records)
        # Byte-identical contract: the stored :text ref still resolvable to the
        # ORIGINAL docling text, unmodified by the gate.
        text_writes = [w for w in fake_redis.writes if w[0] == "set" and ":text" in w[1]]
        assert text_writes, "expected :text artifact ref write"
        ref = text_writes[0][2]
        assert ref.startswith("sha256:")
        stored = FakeStore()
        assert fake_redis.writes, "expected writes"

    def test_hook_runs_on_path_branch(self, monkeypatch, caplog, tmp_path):
        pdf = build_pdf(["Hello world page one"])
        doc = tmp_path / "doc.pdf"
        doc.write_bytes(pdf)
        body = {"job_id": JOB_ID, "document_path": str(doc)}
        w, fake_redis = _integration_worker(monkeypatch)
        with caplog.at_level("INFO", logger="vision.fallback"):
            asyncio.run(
                w._process_message_async(
                    _IncomingMessage(json.dumps(body).encode()), _Channel()
                )
            )
        assert any("quality gate" in r.message for r in caplog.records)
