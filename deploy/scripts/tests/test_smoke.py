"""Assertions del smoke test con fixtures (RED→GREEN)."""
from pathlib import Path

import pytest  # noqa: E402

sys_path = Path(__file__).resolve().parent
import sys  # noqa: E402
sys.path.insert(0, str(sys_path))

from smoke_test import (  # noqa: E402
    build_pdf,
    build_multipart,
    assert_results,
    decode_embedding,
    SmokerFailure,
)


def test_build_pdf_header():
    data = build_pdf("prueba de humo")
    assert data[:8] == b"%PDF-1.4" and b"prueba de humo" in data and data.endswith(b"%%EOF\n")


def test_build_multipart_field():
    body, boundary = build_multipart("doc.pdf", b"%PDF-x", "application/pdf")
    assert boundary.encode() in body and b'name="file"' in body and b"%PDF-x" in body


def test_decode_embedding_gzip_b64_float32le():
    import base64, gzip, struct
    vec = [0.25, -0.5, 1.0]
    blob = struct.pack("<3f", *vec)
    raw = base64.b64encode(gzip.compress(blob)).decode()
    assert decode_embedding(raw) == pytest.approx(vec)


def test_decode_embedding_plain_passthrough():
    assert decode_embedding([1.0, 2.0]) == [1.0, 2.0]


def test_decode_embedding_rejects_bad_size():
    import base64, gzip
    raw = base64.b64encode(gzip.compress(b"abc")).decode()
    with pytest.raises(SmokerFailure, match="múltiplo"):
        decode_embedding(raw)


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
