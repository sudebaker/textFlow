"""Tests for analyze_text text-analysis sampling (spec 16).

Locks that costly statistical features (language detection, readability) run
on text[:TEXT_ANALYSIS_SAMPLE_CHARS] while the boolean regex flags and the
deterministic counts stay on the full text.
"""

import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# langdetect and textstat are production imports of worker.py but are not
# installed in the air-gapped test env; analyze_text never runs them here
# against real implementations.
sys.modules.setdefault("langdetect", MagicMock())
sys.modules.setdefault("textstat", MagicMock())

import worker


class TestAnalyzeTextSampling:
    def test_language_detection_uses_sample(self, monkeypatch):
        worker.langdetect.detect = MagicMock(return_value="en")
        monkeypatch.setattr(worker, "TEXT_ANALYSIS_SAMPLE_CHARS", 10)

        result = worker.analyze_text("a" * 100)

        worker.langdetect.detect.assert_called_once_with("a" * 10)
        assert result["char_count"] == 100

    def test_readability_uses_sample(self, monkeypatch):
        worker.textstat.flesch_reading_ease = MagicMock(return_value=55.5)
        monkeypatch.setattr(worker, "TEXT_ANALYSIS_SAMPLE_CHARS", 20)

        worker.analyze_text("word " * 100)

        (arg,) = worker.textstat.flesch_reading_ease.call_args[0]
        assert len(arg) <= 20

    def test_regex_flags_use_full_text(self, monkeypatch):
        monkeypatch.setattr(worker, "TEXT_ANALYSIS_SAMPLE_CHARS", 10)
        text = "a" * 50 + "https://example.com email@example.com 123"

        result = worker.analyze_text(text)

        # All markers live beyond char 10; full-text scanning must catch them.
        assert result["has_urls"] is True
        assert result["has_emails"] is True
        assert result["has_numbers"] is True

    def test_shape_and_counts_preserved(self, monkeypatch):
        monkeypatch.setattr(worker, "TEXT_ANALYSIS_SAMPLE_CHARS", 10)
        text = "one two\nthree."

        result = worker.analyze_text(text)

        assert set(result) == {
            "char_count",
            "word_count",
            "line_count",
            "language",
            "has_urls",
            "has_emails",
            "has_numbers",
            "encoding",
            "readability_score",
        }
        assert result["char_count"] == len(text)
        assert result["word_count"] == len(text.split())
        assert result["line_count"] == len(text.split("\n"))
