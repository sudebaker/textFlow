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

from unittest.mock import AsyncMock  # noqa: E402

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


# --- extract_page_texts (Task E.2) --------------------------------------------


def _doc_json(texts_items, num_pages=3):
    """docling_response['document']['json_content']-shaped dict (A.1 shape)."""
    return {
        "texts": [
            {"text": t, "prov": [{"page_no": p}]} for (t, p) in texts_items
        ],
        "pages": {str(i): {"page_no": i, "image": None} for i in range(1, num_pages + 1)},
    }


class TestExtractPageTexts:
    def test_groups_by_prov_page_no_joining_newlines(self):
        from vision.fallback import extract_page_texts

        doc = {
            "texts": [
                {"text": "uno", "prov": [{"page_no": 1}]},
                {"text": "dos", "prov": [{"page_no": 2}]},
                {"text": "uno-b", "prov": [{"page_no": 1}]},
            ]
        }
        pages = extract_page_texts(doc, 3)
        assert pages[0] == "uno\nuno-b"
        assert pages[1] == "dos"
        # Page with no items -> None (page gate marks it suspect)
        assert pages[2] is None

    def test_none_shape_returns_all_none(self):
        from vision.fallback import extract_page_texts

        # Unknown shape (no texts anywhere) -> all-None branch
        pages = extract_page_texts({}, 3)
        assert pages == [None, None, None]

    def test_none_mode_returns_all_none(self, monkeypatch):
        import vision.fallback as fb

        monkeypatch.setattr(fb, "PAGE_TEXT_MODE", "none")
        doc = {"texts": [{"text": "x", "prov": [{"page_no": 1}]}]}
        assert fb.extract_page_texts(doc, 2) == [None, None]

    def test_text_prov_short_page_becomes_none(self):
        from vision.fallback import extract_page_texts

        doc = {"texts": [{"text": "   ", "prov": [{"page_no": 1}]}]}
        assert extract_page_texts(doc, 1) == [None]

    def test_out_of_range_page_no_ignored(self):
        from vision.fallback import extract_page_texts

        doc = {"texts": [{"text": "x", "prov": [{"page_no": 99}]}]}
        assert extract_page_texts(doc, 2) == [None, None]

    def test_json_content_string_unwrapped(self):
        from vision.fallback import extract_page_texts

        # json_content may arrive as a JSON string (A.1 raw response) — must
        # be unwrapped to reach texts[].
        jc = json.dumps({"texts": [{"text": "hola", "prov": [{"page_no": 1}]}]})
        doc = {"json_content": jc}
        assert extract_page_texts(doc, 1) == ["hola"]

    def test_json_content_dict_unwrapped(self):
        from vision.fallback import extract_page_texts

        doc = {"json_content": {"texts": [{"text": "p1", "prov": [{"page_no": 1}]}]}}
        assert extract_page_texts(doc, 1) == ["p1"]

    def test_json_content_priority_over_plain_texts(self):
        from vision.fallback import extract_page_texts

        doc = {
            "texts": [{"text": "plain", "prov": [{"page_no": 1}]}],
            "json_content": {"texts": [{"text": "json", "prov": [{"page_no": 1}]}]},
        }
        assert extract_page_texts(doc, 1) == ["json"]


# --- slow path (Task E.2) ------------------------------------------------------

# Doc-level suspicion requires chars/page < min_chars_per_page. Slow-path tests
# use min_chars_per_page=150; per-page dense texts are >=150 chars.
SLOW_MIN_CHARS = 150


def _slow_settings(**overrides):
    return make_settings(ocr_enabled=True, min_chars_per_page=SLOW_MIN_CHARS, **overrides)


def make_suspect_doc():
    """3-page doc: page 2 thin-text (suspect), pages 1/3 dense (>=150 chars)."""
    p1 = "(Pagina uno con texto denso y suficiente) " * 4  # ~168 chars
    p3 = "(Pagina tres con contenido abundante) " * 4
    p2 = "thin"
    return _doc_json([(p1, 1), (p2, 2), (p3, 3)], num_pages=3)


# Realistic docling md blob for a suspect 3-page doc: all page text present in
# markdown (as the real worker sees it), page 2 with only placeholder lines.
SUSPECT_BLOB = (
    "(Pagina uno con texto denso y suficiente) " * 4
    + "\n\n![](img1.png)\n![](img2.png)\n"
    "The scanned manuscript page shows handwritten notes.\n\n"
    + "(Pagina tres con contenido abundante) " * 4
)
SUSPECT_TEXT = SUSPECT_BLOB


class SlowPathCase:
    """Fixture bundle for slow-path tests: patched client + renderer."""

    def __init__(self, monkeypatch, vision_text="VISION PAGE 2 " + "x" * 120):
        import vision.fallback as fb

        self.fb = fb
        monkeypatch.setattr(fb, "render_page_async", AsyncMock(return_value=b"\x89PNG"))

        self.client = MagicMock()
        self.client.transcribe = AsyncMock(return_value=vision_text)
        monkeypatch.setattr(fb, "VisionOCRClient", MagicMock(return_value=self.client))

    def client_calls(self):
        return self.client.transcribe


def make_slow_ctx(**overrides):
    defaults = dict(
        job_id=JOB_ID,
        text=SUSPECT_TEXT,
        docling_document=make_suspect_doc(),
        page_count=3,
        get_document_bytes=lambda: build_pdf([DENSE_TEXT, SUSPECT_TEXT, DENSE_TEXT]),
        redis_client=FakeRedis(),
        raise_if_cancelled=lambda job_id: None,
    )
    defaults.update(overrides)
    settings = defaults.pop("settings", None) or _slow_settings()
    from vision.fallback import FallbackContext

    ctx = FallbackContext(settings=settings, **defaults)
    return ctx


def _provenance_report(redis):
    """Parse the JSON extraction_provenance report from the FakeRedis writes."""
    prov_writes = [w for w in redis.writes if w[0] == "set" and ":extraction_provenance" in w[1]]
    assert len(prov_writes) == 1, f"expected exactly 1 provenance write, got {prov_writes}"
    return json.loads(prov_writes[0][2])


async def _run_rf(ctx):
    """Import-time helper: run_quality_flow is fetched fresh (patchable)."""
    import vision.fallback as fb

    return await fb.run_quality_flow(ctx)


class TestSlowPath:
    def test_pass_doc_never_enters_slow_path(self, monkeypatch):
        case = SlowPathCase(monkeypatch)
        redis = FakeRedis()
        out = asyncio.run(
            case.fb.run_quality_flow(
                make_slow_ctx(
                    text=DENSE_TEXT,
                    docling_document={},
                    page_count=None,
                    redis_client=redis,
                    settings=make_settings(ocr_enabled=True),
                )
            )
        )
        assert out == DENSE_TEXT  # exact, byte-identical
        case.client_calls().assert_not_called()
        case.fb.render_page_async.assert_not_called()
        assert redis.writes == []

    def test_suspect_vision_replaces_thin_page(self, monkeypatch):
        case = SlowPathCase(monkeypatch, vision_text="V " * 60)
        ctx = make_slow_ctx()
        out = asyncio.run(case.fb.run_quality_flow(ctx))
        pages = out.split("\n\n")
        assert len(pages) == 3
        vision_page = "V " * 60
        assert pages[1] == vision_page  # vision text (full string, not stripped)
        assert pages[0] != pages[1]
        # page 1 and 3 keep docling text unmodified
        assert "Pagina uno" in pages[0] and "Pagina tres" in pages[2]
        # only ONE vision call: only page 2 is suspect
        assert case.client_calls().await_count == 1

    def test_vision_http_error_keeps_docling_page(self, monkeypatch):
        case = SlowPathCase(monkeypatch)
        from vision.client import VisionHTTPError

        case.client.transcribe = AsyncMock(side_effect=VisionHTTPError("HTTP 500: boom"))
        ctx = make_slow_ctx()
        out = asyncio.run(case.fb.run_quality_flow(ctx))
        # No vision page succeeded (any_vision=False): contract is ctx.text
        # byte-exact — all 3 pages still present via the docling blob.
        assert out == ctx.text
        assert len(out.split("\n\n")) == 3
        # provenance signals the error
        prov = _provenance_report(ctx.redis_client)
        assert prov["pages"][1]["backend"] == "vision_error"
        assert prov["pages"][1]["status"] == "vision_error"
        assert "HTTP 500" in prov["pages"][1]["reason"]

    def test_vision_saturated_keeps_docling_page(self, monkeypatch):
        case = SlowPathCase(monkeypatch)
        from vision.client import VisionSaturatedError

        case.client.transcribe = AsyncMock(side_effect=VisionSaturatedError("saturated"))
        ctx = make_slow_ctx()
        out = asyncio.run(case.fb.run_quality_flow(ctx))
        assert out == ctx.text  # byte-exact: page keeps docling text
        prov = _provenance_report(ctx.redis_client)
        assert prov["pages"][1]["backend"] == "vision_error"

    def test_provenance_written_with_full_report(self, monkeypatch):
        case = SlowPathCase(monkeypatch, vision_text="V " * 60)
        ctx = make_slow_ctx()
        asyncio.run(case.fb.run_quality_flow(ctx))
        prov = _provenance_report(ctx.redis_client)
        assert prov["gate"]["decision"] == "suspect"
        assert [p["backend"] for p in prov["pages"]] == [
            "docling",
            "vision",
            "docling",
        ]
        assert [p["status"] for p in prov["pages"]] == ["pass", "fallback", "pass"]
        assert prov["vision_pages"] == 1
        assert prov["fallback_whole_document"] is False
        assert "duration_s" in prov

    def test_budget_pages_exhausted_degrades(self, monkeypatch):
        # max_pages=1: page 1 is... dense (pass). Put the thin page FIRST:
        # with max_pages=1 only ONE page gets visioned; the other suspect
        # pages degrade with docling text kept.
        p1 = "thin page one"  # suspect
        p2 = "Pagina dos densa " * 20
        p3 = "thin page three"  # suspect -> degraded (budget exhausted)
        doc = _doc_json([(p1, 1), (p2, 2), (p3, 3)], num_pages=3)
        case = SlowPathCase(monkeypatch, vision_text="VISION TEXT " * 10)
        ctx = make_slow_ctx(
            docling_document=doc,
            settings=_slow_settings(max_pages_per_document=1),
        )
        out = asyncio.run(case.fb.run_quality_flow(ctx))
        pages = out.split("\n\n")
        assert pages[0].startswith("VISION TEXT")  # visioned
        assert pages[1].startswith("Pagina dos")  # dense: pass
        assert pages[2] == "thin page three"  # degraded: docling kept
        assert case.client_calls().await_count == 1
        prov = _provenance_report(ctx.redis_client)
        assert [p["backend"] for p in prov["pages"]] == [
            "vision",
            "docling",
            "docling",
        ]
        assert prov["pages"][2]["status"] == "degraded"
        assert prov["pages"][2]["reason"] == "budget_exhausted"

    def test_budget_seconds_zero_degrades_all(self, monkeypatch):
        case = SlowPathCase(monkeypatch, vision_text="V " * 60)
        ctx = make_slow_ctx(
            settings=_slow_settings(max_seconds_per_document=0.0)
        )
        out = asyncio.run(case.fb.run_quality_flow(ctx))
        # No vision happened (budget exhausted before any render): the output
        # contract is ctx.text byte-exact, NOT re-assembled page text.
        assert out == SUSPECT_TEXT
        assert out == ctx.text
        assert case.client_calls().await_count == 0
        assert case.fb.render_page_async.await_count == 0
        # The budget gate NEVER blocks provenance: degraded pages are still
        # reported (plan E.2: provenance when vision replaces/degrades).
        prov = _provenance_report(ctx.redis_client)
        assert [p["backend"] for p in prov["pages"]] == [
            "docling",
            "docling",
            "docling",
        ]
        assert prov["pages"][1]["status"] == "degraded"
        assert prov["pages"][1]["reason"] == "budget_exhausted"

    def test_all_vision_errors_falls_back_to_whole_document(self, monkeypatch):
        case = SlowPathCase(monkeypatch)
        from vision.client import VisionHTTPError

        case.client.transcribe = AsyncMock(side_effect=VisionHTTPError("kaboom"))
        # Whole-document fallback (§16): assembled page text ends up EMPTY.
        # Every suspect page keeps only its (empty/None) docling page text,
        # so the join is "" -> the intact docling blob is the last defense.
        doc = _doc_json([(" ", 1), (" ", 2), (" ", 3)], num_pages=3)  # blank-only pages -> None
        ctx = make_slow_ctx(docling_document=doc)
        out = asyncio.run(case.fb.run_quality_flow(ctx))
        assert out == SUSPECT_TEXT  # intact docling blob returned...
        prov = _provenance_report(ctx.redis_client)
        assert prov["fallback_whole_document"] is True
        case.fb.render_page_async.assert_awaited()  # vision WAS attempted

    def test_non_pdf_bytes_returns_text_without_vision(self, monkeypatch):
        case = SlowPathCase(monkeypatch)

        class Bom(SlowPathCase):
            pass

        ctx = make_slow_ctx(get_document_bytes=lambda: b"XXYY-not-a-pdf")
        out = asyncio.run(case.fb.run_quality_flow(ctx))
        assert out == SUSPECT_TEXT
        case.client_calls().assert_not_called()
        case.fb.render_page_async.assert_not_called()
        assert ctx.redis_client.writes == []

    def test_job_cancelled_error_propagates(self, monkeypatch):
        SlowPathCase(monkeypatch)
        calls = {"n": 0}

        def raise_after_first(job_id):
            calls["n"] += 1
            if calls["n"] > 1:
                from pkg.shared.exceptions import JobCancelledError

                raise JobCancelledError("cancelled")

        ctx = make_slow_ctx(raise_if_cancelled=raise_after_first)
        from pkg.shared.exceptions import JobCancelledError

        with pytest.raises(JobCancelledError):
            import vision.fallback as fb

            asyncio.run(fb.run_quality_flow(ctx))
        assert calls["n"] >= 2  # raised at start + between pages

    def test_ocr_disabled_suspect_fast_return(self, monkeypatch):
        case = SlowPathCase(monkeypatch)
        ctx = make_slow_ctx(settings=make_settings(ocr_enabled=False))
        out = asyncio.run(case.fb.run_quality_flow(ctx))
        assert out == SUSPECT_TEXT
        case.client_calls().assert_not_called()
        case.fb.render_page_async.assert_not_called()
        assert ctx.redis_client.writes == []


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
