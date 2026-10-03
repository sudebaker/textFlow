"""Tests for vision.gate: heuristic document/page quality gate (spec extraccion-visual §8).

Thresholds are INITIAL heuristics — to be calibrated with real-traffic metrics
(Phase C) before enabling the fallback (Phase F).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from vision.config import VisionSettings  # noqa: E402

import dataclasses  # noqa: E402


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


DENSE_ES = (
    "El contrato de arrendamiento se firma en Madrid el tres de marzo. "
    "El inquilino se compromete al pago mensual de la renta acordada. "
    "Cualquier modificación deberá notificarse por escrito con treinta días "
    "de antelación, conforme a lo dispuesto en la cláusula quinta. "
    "En caso de incumplimiento, el arrendador podrá iniciar procedimientos. "
)


# --- compute_signals -------------------------------------------------------


class TestComputeSignals:
    def test_dense_text_ratio_zero(self):
        from vision.gate import compute_signals

        s = compute_signals(DENSE_ES, 1)
        assert s.image_placeholder_ratio == 0.0
        assert s.garbage_ratio == 0.0

    def test_page_count_none_treated_as_one(self):
        from vision.gate import compute_signals

        single = compute_signals(DENSE_ES, None)
        as_one = compute_signals(DENSE_ES, 1)
        assert single.chars_per_page == as_one.chars_per_page == float(len(DENSE_ES))

    def test_chars_per_page_uses_max(self):
        from vision.gate import compute_signals

        s = compute_signals("1234567890" * 20, 4)
        assert s.chars_per_page == pytest.approx(len("1234567890" * 20) / 4)


# --- evaluate_document -----------------------------------------------------


class TestEvaluateDocument:
    def test_dense_text_passes(self):
        from vision.gate import compute_signals, evaluate_document

        settings = make_settings()
        signals = compute_signals(DENSE_ES, 1)
        assert evaluate_document(signals, settings) == "pass"

    @pytest.mark.parametrize("text", ["", "   ", "\n\n"])
    def test_empty_text_suspect(self, text):
        from vision.gate import compute_signals, evaluate_document

        signals = compute_signals(text, 2)
        assert evaluate_document(signals, make_settings()) == "suspect"

    def test_image_placeholder_markdown_suspect(self):
        from vision.gate import compute_signals, evaluate_document

        # 4 placeholder lines vs 1 short text line => ~80% placeholders
        placeholder_lines = [
            "![cover image]()",
            "![](assets/fig1.png)",
            "![](assets/fig2.png)",
            "![](assets/fig3.png)",
            "scanned",
        ]
        text = "\n".join(placeholder_lines)
        settings = make_settings()
        signals = compute_signals(text, 1)
        assert signals.image_placeholder_ratio == pytest.approx(4 / 5)
        assert evaluate_document(signals, settings) == "suspect"

    @pytest.mark.parametrize(
        "garbage_text",
        [
            "(cid:1)\ufffd",  # cid glyphs + replacement chars, dense
            "\ufffd\ufffd",  # replacement chars alone: count garbage+non_printable
        ],
    )
    def test_garbage_text_suspect(self, garbage_text):
        from vision.gate import compute_signals, evaluate_document

        text = garbage_text * 100  # high garbage density over 2 pages
        settings = make_settings()
        signals = compute_signals(text, 2)
        assert signals.garbage_ratio > settings.garbage_ratio_max
        assert evaluate_document(signals, settings) == "suspect"

    def test_garbage_re_counts_cid_and_replacement(self):
        from vision.gate import compute_signals

        s = compute_signals("(cid:7)\ufffd", 1)
        # garbage matches: "(cid:7)" + "\ufffd" = 2; non_printable: "\ufffd" = 1
        assert s.garbage_ratio == pytest.approx(3 / 8)

    def test_scan_without_ocr_suspect(self):
        from vision.gate import compute_signals, evaluate_document

        # A scanned PDF without OCR: docling returns only empty-image placeholders.
        text = "![page1]()\n![page2]()\n![page3]()\n![page4]()"
        settings = make_settings()
        signals = compute_signals(text, 4)
        assert evaluate_document(signals, settings) == "suspect"

    def test_low_chars_per_page_suspect(self):
        from vision.gate import compute_signals, evaluate_document

        settings = make_settings(min_chars_per_page=100)
        text = "too short"  # 9 chars over 2 pages => 4.5 chars/page
        signals = compute_signals(text, 2)
        assert evaluate_document(signals, settings) == "suspect"


# --- evaluate_page ---------------------------------------------------------


class TestEvaluatePage:
    def test_dense_page_passes(self):
        from vision.gate import evaluate_page

        assert evaluate_page(DENSE_ES, make_settings()) == "pass"

    def test_none_page_suspect(self):
        from vision.gate import evaluate_page

        assert evaluate_page(None, make_settings()) == "suspect"

    def test_short_page_suspect(self):
        from vision.gate import evaluate_page

        assert evaluate_page("short", make_settings()) == "suspect"

    def test_page_exactly_at_threshold_passes(self):
        from vision.gate import evaluate_page

        settings = make_settings(min_chars_per_page=100)
        page = "a" * 100
        assert evaluate_page(page, settings) == "pass"

    def test_page_below_threshold_suspect(self):
        from vision.gate import evaluate_page

        settings = make_settings(min_chars_per_page=100)
        page = "a" * 20
        assert evaluate_page(page, settings) == "suspect"


# --- GateSignals ------------------------------------------------------------


class TestGateSignals:
    def test_dataclass_fields(self):
        from vision.gate import GateSignals

        fields = {f.name for f in dataclasses.fields(GateSignals)}
        assert fields == {
            "page_count",
            "char_count",
            "chars_per_page",
            "text_density",
            "image_placeholder_ratio",
            "garbage_ratio",
        }
