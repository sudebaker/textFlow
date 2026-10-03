"""Tests for vision.provenance: per-page extraction provenance Redis writer (spec §17)."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from golden_utils import FakeRedis  # noqa: E402
from vision.provenance import write_provenance  # noqa: E402

REPORT = {
    "gate": {"decision": "suspect", "chars_per_page": 3.5},
    "pages": [{"page_no": 1, "backend": "vision", "status": "fallback"}],
    "vision_pages": 1,
    "duration_s": 1.23,
    "fallback_whole_document": False,
}


class TestWriteProvenance:
    def test_exact_key_and_json_roundtrip(self):
        redis = FakeRedis()
        write_provenance(redis, "test-job", REPORT)
        assert len(redis.writes) == 1
        op, key, value = redis.writes[0]
        assert op == "set"
        assert key == "orchestrator:job:test-job:extraction_provenance"
        assert json.loads(value) == REPORT

    def test_nothing_else_written(self):
        redis = FakeRedis()
        write_provenance(redis, "test-job", {"pages": []})
        # Only one op, and it is a plain set (no hset/publish side effects).
        assert [w[0] for w in redis.writes] == ["set"]
        assert [w[1] for w in redis.writes] == ["orchestrator:job:test-job:extraction_provenance"]

    def test_returns_none(self):
        assert write_provenance(FakeRedis(), "test-job", REPORT) is None
