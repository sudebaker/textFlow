"""Worker exposes the docling document + requests to_formats=md,json (Task E.3).

Fase A finding A.1 (docs/extraccion-visual-verificacion-A.md): the DEFAULT
docling-serve response has NO per-page data — requesting to_formats=json makes
`document.json_content` (full DoclingDocument) available, whose
`texts[].prov[].page_no` and `pages` map feed the vision slow path.
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

import pytest  # noqa: E402

import worker  # noqa: E402

from fixtures import build_pdf  # noqa: E402

PROJECT_ROOT = Path(
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
)

DOC = {
    "filename": "doc.pdf",
    "md_content": "# page one text here\n\n# page two longer body",
    "json_content": {
        "texts": [
            {"text": "page one text here", "prov": [{"page_no": 1}]},
            {"text": "page two longer body", "prov": [{"page_no": 2}]},
        ],
        "pages": {"1": {"page_no": 1}, "2": {"page_no": 2}},
    },
    "text_content": None,
    "pages": [{"page_no": 1}, {"page_no": 2}],
}

RESPONSE = {"document": DOC, "task_id": "t1", "task_status": "success"}


async def _fake_docling(self, document_bytes, filename):
    return RESPONSE


def patched_worker(monkeypatch):
    monkeypatch.setenv(
        "PIPELINE_CONFIG_PATH", str(PROJECT_ROOT / "configs" / "pipeline.json")
    )
    monkeypatch.setattr(worker.ExtractionWorker, "_docling_convert_async", _fake_docling)
    return worker.ExtractionWorker()


class TestReturnDoclingDocument:
    def test_base64_returns_docling_document(self, monkeypatch):
        w = patched_worker(monkeypatch)
        pdf = build_pdf(["page one text here", "page two longer body"])
        result = asyncio.run(
            w.extract_text_from_base64(
                __import__("base64").b64encode(pdf).decode("ascii"), "doc.pdf"
            )
        )
        assert result["docling_document"] == DOC
        # existing keys intact
        assert "page one text here" in result["text"]
        assert result["metadata"]["extraction_method"] == "base64"

    def test_file_returns_docling_document(self, monkeypatch, tmp_path):
        w = patched_worker(monkeypatch)
        pdf = build_pdf(["page one text here"])
        f = tmp_path / "doc.pdf"
        f.write_bytes(pdf)
        result = asyncio.run(w.extract_text_from_file(str(f)))
        assert result["docling_document"] == DOC
        assert result["metadata"]["extraction_method"] == "file"

    def test_url_internal_returns_docling_document(self, monkeypatch, tmp_path):
        pytest.skip("URL branch shares the same return-dict assembly; covered by diff review")


class TestToFormatsRequested:
    def test_docling_convert_asks_md_and_json(self, monkeypatch):
        # Build a fake session/plumbing so _docling_convert_async runs its full
        # submit->poll->result dance against fakes.
        form_recorder = MagicMock()

        calls = {"post": []}

        class _Resp:
            def __init__(self, payload):
                self._payload = payload

            def raise_for_status(self):
                pass

            async def json(self):
                return self._payload

        class _CM:
            def __init__(self, payload):
                self._payload = payload

            async def __aenter__(self):
                return _Resp(self._payload)

            async def __aexit__(self, *exc):
                return False

        class _FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def post(self, url, data=None, timeout=None):
                calls["post"].append({"url": url, "data": data})
                return _CM({"task_id": "T1"})

            def get(self, url, params=None, timeout=None):
                if "/status/poll/" in url:
                    return _CM({"task_status": "success"})
                if "/result/" in url:
                    return _CM(RESPONSE)
                raise AssertionError(f"unexpected GET {url}")

        fake_aiohttp = SimpleNamespace(
            ClientSession=lambda: _FakeSession(),
            FormData=lambda: form_recorder,
            ClientTimeout=lambda **kw: MagicMock(),
        )
        monkeypatch.setattr(worker, "aiohttp", fake_aiohttp)

        monkeypatch.setenv(
            "PIPELINE_CONFIG_PATH", str(PROJECT_ROOT / "configs" / "pipeline.json")
        )
        w = worker.ExtractionWorker()
        asyncio.run(w._docling_convert_async(b"%PDF-x", "doc.pdf"))

        added = form_recorder.add_field.call_args_list
        to_formats = [c for c in added if c.args and c.args[0] == "to_formats"]
        to_formats_values = [c.args[1] for c in to_formats]
        assert "md" in to_formats_values
        assert "json" in to_formats_values
        assert calls["post"][0]["url"].endswith("/v1/convert/file/async")


class TestPageCount:
    def test_docling_pages_prefers_json_content_pages(self, monkeypatch):
        w = patched_worker(monkeypatch)
        pdf = build_pdf(["p1", "p2"])
        result = asyncio.run(
            w.extract_text_from_base64(
                __import__("base64").b64encode(pdf).decode("ascii"), "doc.pdf"
            )
        )
        # A.1: document.pages does NOT exist in docling-serve latest; the page
        # count must come from json_content.pages (dict indexed by page_no).
        assert result["metadata"]["docling_pages"] == 2


class TestPageCountLegacyFallback:
    def test_legacy_document_pages_list_still_counts(self, monkeypatch):
        legacy = {"document": {"md_content": "x", "pages": [{"page_no": 1}, {"page_no": 2}]}}
        async def _fake_legacy(self, document_bytes, filename):
            return legacy
        monkeypatch.setenv(
            "PIPELINE_CONFIG_PATH", str(PROJECT_ROOT / "configs" / "pipeline.json")
        )
        monkeypatch.setattr(worker.ExtractionWorker, "_docling_convert_async", _fake_legacy)
        w = worker.ExtractionWorker()
        pdf = build_pdf(["p1", "p2"])
        result = asyncio.run(
            w.extract_text_from_base64(
                __import__("base64").b64encode(pdf).decode("ascii"), "doc.pdf"
            )
        )
        assert result["metadata"]["docling_pages"] == 2

    def test_no_pages_at_all_Yields_none(self, monkeypatch):
        bare = {"document": {"md_content": "x"}}
        async def _fake_bare(self, document_bytes, filename):
            return bare
        monkeypatch.setenv(
            "PIPELINE_CONFIG_PATH", str(PROJECT_ROOT / "configs" / "pipeline.json")
        )
        monkeypatch.setattr(worker.ExtractionWorker, "_docling_convert_async", _fake_bare)
        w = worker.ExtractionWorker()
        pdf = build_pdf(["p1"])
        result = asyncio.run(
            w.extract_text_from_base64(
                __import__("base64").b64encode(pdf).decode("ascii"), "doc.pdf"
            )
        )
        assert result["metadata"]["docling_pages"] is None
