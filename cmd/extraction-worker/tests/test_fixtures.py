"""Deterministic minimal-PDF builder (no binary fixtures in git)."""

import pytest

from fixtures import build_pdf  # noqa: F401  (tests add their dir to sys.path)


class TestBuildPdfStructure:
    def test_build_pdf_two_pages(self):
        pdf = build_pdf(["Hello world page one", "Second page text here"])
        assert pdf[:8] == b"%PDF-1.4"
        assert pdf.rstrip().endswith(b"%%EOF")
        assert b"/Count 2" in pdf
        assert b"Hello world page one" in pdf and b"Second page text here" in pdf

    def test_build_pdf_single_page(self):
        pdf = build_pdf(["Only one page"])
        assert b"/Count 1" in pdf
        assert b"Only one page" in pdf

    def test_trailer_size_and_startxref(self):
        # 2 pages -> objects 1(catalog) 2(pages) 3/4 5/6 (page/content) 7(font) => Size 8
        pdf = build_pdf(["a", "b"])
        assert b"/Size 8" in pdf
        assert b"startxref" in pdf

    def test_escapes_pdf_literal_strings(self):
        pdf = build_pdf(["a (b) \\ c"])
        assert b"a \\(b\\) \\\\ c" in pdf

    def test_empty_pages_rejected(self):
        with pytest.raises(ValueError):
            build_pdf([])


class TestBuildPdfWithPypdfium2:
    """Validated with a real PDF parser when pypdfium2 is installed locally.

    Skips silently in the air-gapped test env (pypdfium2 not installed)."""

    def test_parses_pages_and_text(self):
        import sys
        import unittest.mock

        pytest.importorskip("pypdfium2")
        # test_golden_vision_off stubs pypdfium2 module-wide for later phases;
        # skip if the entry in sys.modules is a stub rather than the real lib.
        stubbed = isinstance(sys.modules.get("pypdfium2"), unittest.mock.Mock)
        if stubbed:
            pytest.skip("pypdfium2 stubbed by golden tests (not installed here)")
        pypdfium2 = __import__("pypdfium2")
        pdf = build_pdf(["Hello world page one", "Second page text here"])
        doc = pypdfium2.PdfDocument(pdf)
        try:
            assert len(doc) == 2
            page_texts = []
            for page in doc:
                text_page = page.get_textpage()
                page_texts.append(text_page.get_text_bounded())
                text_page.close()
                page.close()
        finally:
            doc.close()
        assert "Hello world page one" in page_texts[0]
        assert "Second page text here" in page_texts[1]
