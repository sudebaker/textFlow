"""Assertions del smoke test con fixtures (RED→GREEN)."""
from pathlib import Path

import pytest  # noqa: E402

sys_path = Path(__file__).resolve().parent
import sys  # noqa: E402
sys.path.insert(0, str(sys_path))

from smoke_test import (  # noqa: E402
    build_pdf,
    assert_results,
    SmokerFailure,
)


def test_build_pdf_header():
    data = build_pdf("prueba de humo")
    assert data[:8] == b"%PDF-1.4" and b"prueba de humo" in data and data.endswith(b"%%EOF\n")


def _results(status="completed", dims=768):
    return {
        "status": status,
        "steps": {"extraction": "completed", "metadata": "completed",
                  "embeddings": "completed"},
        "text": "algo de texto",
        "document_metadata": {"mime_type": "application/pdf"},
        "chunks": [{"chunk_id": "c1", "embeddings": [0.1] * dims}],
        "entities": {},
    }


def test_assert_results_pass_complete():
    assert_results(_results(), expected_dims=768, required=("extraction", "metadata", "embeddings"))


def test_assert_results_dims_mismatch_fails():
    with pytest.raises(SmokerFailure, match="768"):
        assert_results(_results(dims=1024), expected_dims=768,
                       required=("extraction", "metadata", "embeddings"))


def test_assert_results_failed_job():
    with pytest.raises(SmokerFailure, match="failed"):
        assert_results(_results(status="failed"), expected_dims=768, required=())


def test_assert_results_missing_step_fails():
    r = _results(); del r["steps"]["embeddings"]
    with pytest.raises(SmokerFailure, match="embeddings"):
        assert_results(r, expected_dims=768, required=("embeddings",))
    r = _results(); r["steps"]["embeddings"] = "queued"
    with pytest.raises(SmokerFailure, match="queued"):
        assert_results(r, expected_dims=768, required=("embeddings",))


def test_assert_results_no_chunks_fails():
    r = _results(); r["chunks"] = []
    with pytest.raises(SmokerFailure, match="chunks"):
        assert_results(r, expected_dims=768, required=())


def test_assert_results_warns_empty_entities(capsys):
    r = _results(); r["entities"] = {}
    assert_results(r, expected_dims=768, required=())   # warning, no excepción
    out = capsys.readouterr().err + capsys.readouterr().out
    assert "entities" in out
