"""Unit tests for tools/bench-vision-ocr.py pure helpers (no live service).

Runs standalone: pytest tools/test_bench_vision_ocr.py
(repo test targets only cover cmd/*/tests + deploy/docker/vision-ocr/tests;
this suite is invoked explicitly, not part of make test-python targets).
"""

import csv
import importlib.util
import os
import sys
import unittest.mock

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
_SPEC = importlib.util.spec_from_file_location(
    "bench_tool", os.path.join(HERE, "bench-vision-ocr.py")
)
bench = importlib.util.module_from_spec(_SPEC)
sys.path.insert(0, HERE)
_SPEC.loader.exec_module(bench)


def test_parse_args_defaults(monkeypatch):
    monkeypatch.delenv("VISION_OCR_URL", raising=False)
    args = bench.parse_args([])
    assert args.corpus == "corpus/*.pdf"
    assert args.dpi_list == [120, 200, 300]
    assert args.url == "http://localhost:8080"
    assert args.out == "bench_vision.csv"
    assert args.limit_pages == 0


def test_parse_args_env_and_cli(monkeypatch):
    monkeypatch.setenv("VISION_OCR_URL", "http://envhost:9000")
    args = bench.parse_args([])
    assert args.url == "http://envhost:9000"
    args = bench.parse_args(
        ["--url", "http://clihost:1", "--dpi", "150", "--limit-pages", "3"]
    )
    assert args.url == "http://clihost:1"
    assert args.dpi_list == [150]
    assert args.limit_pages == 3


def test_build_row_success():
    row = bench.build_row(
        file="f.pdf",
        page=2,
        dpi=200,
        response_json={
            "extracted_text": "hello world\n second line",
            "elapsed_ms": 123,
            "cached": False,
            "usage": {"prompt_tokens": 7, "completion_tokens": 11},
        },
        wall_ms=150.4,
    )
    assert row == {
        "file": "f.pdf",
        "page": "2",
        "dpi": "200",
        "latency_ms": "123",
        "wall_ms": "150.4",
        "chars": "24",
        "words": "4",
        "tokens_prompt": "7",
        "tokens_completion": "11",
        "cached": "False",
        "error": "",
    }


def test_build_row_error():
    row = bench.build_row(
        file="f.pdf", page=1, dpi=120, response_json=None, error="boom"
    )
    assert row["error"] == "boom"
    for k in ("latency_ms", "chars", "words", "tokens_prompt", "tokens_completion"):
        assert row[k] == ""
    assert row["wall_ms"] == ""
    assert row["file"] == "f.pdf" and row["page"] == "1" and row["dpi"] == "120"


def test_build_row_missing_extracted_text():
    row = bench.build_row(
        file="f.pdf", page=1, dpi=120, response_json={"elapsed_ms": 5}
    )
    assert "extracted_text" in row["error"]
    assert row["chars"] == ""


def test_csv_round_trip(tmp_path):
    rows = [
        bench.build_row("a.pdf", 1, 200, {"extracted_text": "hi", "elapsed_ms": 9}, wall_ms=10),
        bench.build_row("a.pdf", 2, 200, None, error="err"),
    ]
    out = tmp_path / "out.csv"
    bench.write_csv(str(out), rows)
    with open(out, newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        data = list(reader)
    assert header == bench.CSV_COLUMNS
    assert len(data) == 2
    assert data[0] == ["a.pdf", "1", "200", "9", "10.0", "2", "1", "", "", "False", ""]
    assert data[1][10] == "err"


def _try_render_test():
    """Import fixtures + renderer; returns fn or None if not importable."""
    try:
        sys.path.insert(0, os.path.join(HERE, "..", "cmd", "extraction-worker", "tests"))
        from fixtures import build_pdf

        import pypdfium2  # noqa: F401
    except ImportError:
        return None
    return build_pdf


@pytest.mark.skipif(_try_render_test() is None, reason="pypdfium2/fixtures unavailable")
def test_render_smoke():
    build_pdf = _try_render_test()
    sys.path.insert(0, os.path.join(HERE, "..", "cmd", "extraction-worker"))
    from vision.renderer import render_page

    png = render_page(build_pdf(["hello page"]), 1, dpi=72)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
