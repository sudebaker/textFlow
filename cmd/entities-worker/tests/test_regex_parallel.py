"""Unit tests for extract_regex_parallel and regex settings wiring."""

import json
import time
from unittest.mock import MagicMock

import entities_worker as ew


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
