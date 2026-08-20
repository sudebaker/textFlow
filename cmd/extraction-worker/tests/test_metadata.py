"""Tests for fast/deep document metadata extraction."""

import hashlib
import json
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Third-party deps of worker.py are not all installed in the air-gapped test
# env. Stub them before importing so the module loads; tests patch the real
# functions under test explicitly.
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
):
    sys.modules.setdefault(_mod, MagicMock())

from worker import extract_metadata_deep, extract_metadata_fast  # noqa: E402

EXPECTED_KEYS = {
    "filename",
    "file_size_bytes",
    "sha256",
    "author",
    "title",
    "subject",
    "creator",
    "producer",
    "creation_date",
    "modification_date",
    "page_count",
    "encrypted",
    "mime_type",
    "exif_data",
}


class TestExtractMetadataFast:
    def test_full_shape_and_hash(self, tmp_path):
        f = tmp_path / "doc.pdf"
        f.write_bytes(b"hello world")

        with patch("worker.magic.from_file", return_value="application/pdf") as m_from_file:
            md = extract_metadata_fast(str(f), "doc.pdf")

        assert set(md.keys()) == EXPECTED_KEYS
        assert md["filename"] == "doc.pdf"
        assert md["file_size_bytes"] == 11
        assert (
            md["sha256"]
            == "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
        )
        assert md["mime_type"] == "application/pdf"
        assert md["author"] is None
        assert md["page_count"] is None
        assert md["encrypted"] is False
        assert md["exif_data"] == {}
        m_from_file.assert_called_once_with(str(f), mime=True)

    def test_missing_file_returns_defaults(self, tmp_path):
        md = extract_metadata_fast(str(tmp_path / "nope.pdf"), "nope.pdf")
        assert md["file_size_bytes"] == 0
        assert md["sha256"] == ""
        assert md["mime_type"] is None


class TestExtractMetadataDeep:
    def test_enriches_metadata_in_place(self, tmp_path):
        f = tmp_path / "doc.pdf"
        f.write_bytes(b"data")
        md = extract_metadata_fast(str(f), "doc.pdf")

        exif_json = json.dumps(
            [
                {
                    "SourceFile": "x.pdf",
                    "Author": "Alice",
                    "Title": "My Doc",
                    "Subject": "Notes",
                    "Creator": "Tool",
                    "Producer": "Lib",
                    "CreateDate": "2024:01:01 00:00:00",
                    "PageCount": "5",
                    "Encrypted": "Yes",
                    "File:FileSize": "123",
                    "Extra": "kept",
                }
            ]
        )

        with patch("worker.subprocess.run") as m_run:
            m_run.return_value = MagicMock(returncode=0, stdout=exif_json)
            result = extract_metadata_deep(str(f), md)

        assert result is md
        assert md["author"] == "Alice"
        assert md["title"] == "My Doc"
        assert md["subject"] == "Notes"
        assert md["creator"] == "Tool"
        assert md["producer"] == "Lib"
        assert md["creation_date"] == "2024:01:01 00:00:00"
        assert md["page_count"] == 5
        assert md["encrypted"] == "Yes"
        assert md["exif_data"]["Extra"] == "kept"
        assert "SourceFile" not in md["exif_data"]
        assert "File:FileSize" not in md["exif_data"]
        m_run.assert_called_once()

    def test_exiftool_failure_keeps_fast_values(self, tmp_path):
        f = tmp_path / "doc.pdf"
        f.write_bytes(b"data")
        md = extract_metadata_fast(str(f), "doc.pdf")

        with patch("worker.subprocess.run", side_effect=Exception("boom")):
            extract_metadata_deep(str(f), md)

        assert md["author"] is None
        assert md["exif_data"] == {}

    def test_empty_exif_output_leaves_deep_fields_none(self, tmp_path):
        f = tmp_path / "doc.pdf"
        f.write_bytes(b"data")
        md = extract_metadata_fast(str(f), "doc.pdf")

        with patch("worker.subprocess.run") as m_run:
            m_run.return_value = MagicMock(returncode=0, stdout="[]")
            extract_metadata_deep(str(f), md)

        assert md["author"] is None
        assert md["page_count"] is None
