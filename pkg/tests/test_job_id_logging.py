"""
Tests for job_id propagation in Python worker log records.

Verifies that BaseWorker._extract_job_id correctly extracts job identifiers
from message bodies for use in structured log records.
"""

# Standard library
import json
import logging
from unittest.mock import MagicMock, patch

# Third-party
import pytest

# Local
from pkg.worker_common.base import BaseWorker


class ConcreteWorker(BaseWorker):
    """Minimal concrete subclass for testing BaseWorker methods."""

    def __init__(self):
        with (
            patch("pkg.worker_common.base.start_http_server"),
            patch.object(BaseWorker, "_init_health_server"),
            patch("pkg.worker_common.base.Counter"),
            patch("pkg.worker_common.base.Histogram"),
            patch("pkg.worker_common.base.Gauge"),
        ):
            super().__init__(
                worker_name="test-worker",
                queue_name="test_queue",
                metrics_port=9999,
            )

    def process_message(self, message):
        return None


@pytest.fixture
def worker():
    return ConcreteWorker()


class TestExtractJobId:
    def test_extract_job_id_from_body_with_job_id_field(self, worker):
        body = json.dumps({"job_id": "abc-123", "data": "some data"}).encode()
        assert worker._extract_job_id(body) == "abc-123"

    def test_extract_job_id_from_body_with_id_field(self, worker):
        body = json.dumps({"id": "xyz-456", "data": "some data"}).encode()
        assert worker._extract_job_id(body) == "xyz-456"

    def test_extract_job_id_returns_none_for_invalid_json(self, worker):
        assert worker._extract_job_id(b"not json") is None

    def test_extract_job_id_returns_none_when_no_id_field(self, worker):
        body = json.dumps({"data": "x"}).encode()
        assert worker._extract_job_id(body) is None

    def test_extract_job_id_prefers_job_id_over_id(self, worker):
        """When both 'job_id' and 'id' are present, 'job_id' takes precedence."""
        body = json.dumps({"job_id": "primary-id", "id": "secondary-id"}).encode()
        assert worker._extract_job_id(body) == "primary-id"

    def test_extract_job_id_returns_none_for_empty_body(self, worker):
        assert worker._extract_job_id(b"") is None

    def test_extract_job_id_returns_none_for_empty_object(self, worker):
        body = json.dumps({}).encode()
        assert worker._extract_job_id(body) is None


class TestJobIdInLogs:
    def test_on_message_logs_include_job_id(self, worker):
        """All log records during message processing include job_id in extra."""
        log_records = []

        class CapturingHandler(logging.Handler):
            def emit(self, record):
                log_records.append(record)

        handler = CapturingHandler()
        worker.logger.addHandler(handler)

        body = json.dumps({"job_id": "test-job-99", "text": "hello"}).encode()
        ch = MagicMock()
        method = MagicMock()
        properties = MagicMock()
        properties.headers = None

        with patch.object(worker, "process_message", return_value=None):
            with patch.object(worker, "_on_message_processed"):
                with patch.object(worker, "job_duration"):
                    with patch.object(worker, "jobs_total"):
                        worker._on_message(ch, method, properties, body)

        worker.logger.removeHandler(handler)

        # At least one log record related to job completion must include job_id
        job_id_records = [
            r for r in log_records if getattr(r, "job_id", None) == "test-job-99"
        ]
        assert len(job_id_records) > 0, (
            f"Expected at least one log record with job_id='test-job-99', "
            f"got records: {[(r.getMessage(), r.__dict__) for r in log_records]}"
        )
