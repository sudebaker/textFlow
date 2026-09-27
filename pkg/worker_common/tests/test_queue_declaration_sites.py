"""Tests: every Python queue declaration site emits the shared arguments table.

The Go orchestrator declares the pipeline queues with DLX plus conditional
length-limit arguments (internal/broker/rabbitmq.go:declareQueue). Any Python
site declaring the same queues with a different table triggers RabbitMQ
PRECONDITION_FAILED ("inequivalent arg") reconnection loops — the bug class
documented in AGENTS.md. These tests pin the exact table each site passes to
queue_declare / declare_queue, with the broker mocked.
"""

# Standard library
import asyncio
import sys
import types
import uuid
from contextlib import contextmanager
from unittest.mock import MagicMock

# Third-party
import pytest

# aio_pika is not installed in the air-gapped test env; stub it (and its
# submodule) before importing modules that need it at import time — same
# convention as cmd/extraction-worker/tests/test_metadata.py.
for _mod in ("aio_pika", "aio_pika.abc"):
    sys.modules.setdefault(_mod, MagicMock())

# Local
from pkg.worker_common.async_base import BaseAsyncWorker
from pkg.worker_common.base import BaseWorker
from pkg.worker_common.rabbitmq import declare_queue
from pkg.worker_common.rabbitmq_async import declare_queue_async

QUEUE_NAME = "extract_text"
DLX_EXCHANGE = "document_processor_dlx"


def _unique_worker_name(prefix: str) -> str:
    """Unique name per worker instance (prometheus forbids duplicate metrics)."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _capture_sync_utility(monkeypatch, queue_name):
    """Table passed by declare_queue (pkg/worker_common/rabbitmq.py)."""
    channel = MagicMock()
    declare_queue(channel, queue_name)
    return channel.queue_declare.call_args.kwargs["arguments"]


def _capture_async_utility(monkeypatch, queue_name):
    """Table passed by declare_queue_async (pkg/worker_common/rabbitmq_async.py)."""
    recorded = {}

    class FakeChannel:
        async def declare_queue(self, name, durable=True, arguments=None):
            recorded["arguments"] = arguments
            return object()

    asyncio.run(declare_queue_async(FakeChannel(), queue_name))
    return recorded["arguments"]


def _capture_base_worker_run(monkeypatch, queue_name):
    """Table passed by BaseWorker.run (pkg/worker_common/base.py)."""
    from pkg.worker_common import base as base_module

    worker = base_module.BaseWorker(
        _unique_worker_name("bw"), queue_name, metrics_port=8765
    )
    recorded = {}

    class RecordingChannel:
        def queue_declare(self, queue, durable=True, arguments=None):
            recorded["arguments"] = arguments

        def basic_consume(self, queue, on_message_callback, auto_ack):
            pass

        def start_consuming(self):
            worker._shutdown_requested = True

    @contextmanager
    def fake_rabbitmq_connection(url):
        yield object(), RecordingChannel()

    monkeypatch.setattr(base_module, "rabbitmq_connection", fake_rabbitmq_connection)
    monkeypatch.setattr(worker, "start_servers", lambda: None)
    worker.run()
    return recorded["arguments"]


def _capture_async_base_connect(monkeypatch, queue_name):
    """Table passed by BaseAsyncWorker.connect_rabbitmq (async_base.py)."""
    from pkg.worker_common import async_base as async_base_module

    worker = async_base_module.BaseAsyncWorker(
        _unique_worker_name("abw"), queue_name, metrics_port=8766
    )
    recorded = {}

    class FakeChannel:
        async def set_qos(self, prefetch_count=None):
            pass

        async def declare_queue(self, name, durable=True, arguments=None):
            recorded["arguments"] = arguments
            return object()

    class FakeConnection:
        def __init__(self, channel):
            self._channel = channel

        async def channel(self):
            return self._channel

    async def fake_connect_robust(url):
        return FakeConnection(FakeChannel())

    monkeypatch.setitem(
        sys.modules, "aio_pika", types.SimpleNamespace(connect_robust=fake_connect_robust)
    )
    asyncio.run(worker.connect_rabbitmq())
    return recorded["arguments"]


DECLARATION_SITES = [
    _capture_sync_utility,
    _capture_async_utility,
    _capture_base_worker_run,
    _capture_async_base_connect,
]

SITE_IDS = [
    "rabbitmq-declare_queue",
    "rabbitmq_async-declare_queue_async",
    "base-BaseWorker-run",
    "async_base-connect_rabbitmq",
]


def assert_dlx_pair(arguments, queue_name):
    assert arguments["x-dead-letter-exchange"] == DLX_EXCHANGE
    assert arguments["x-dead-letter-routing-key"] == f"{queue_name}_failed"


@pytest.mark.parametrize("capture", DECLARATION_SITES, ids=SITE_IDS)
def test_declaration_includes_limit_args_by_default(monkeypatch, capture):
    monkeypatch.delenv("QUEUE_MAX_LENGTH", raising=False)
    arguments = capture(monkeypatch, QUEUE_NAME)
    assert_dlx_pair(arguments, QUEUE_NAME)
    assert arguments.get("x-max-length") == 1000, (
        f"declaration table must include x-max-length=1000 by default, got: {arguments}"
    )
    assert isinstance(arguments["x-max-length"], int)
    assert arguments.get("x-overflow") == "reject-publish", (
        f"declaration table must include x-overflow=reject-publish, got: {arguments}"
    )


@pytest.mark.parametrize("capture", DECLARATION_SITES, ids=SITE_IDS)
def test_declaration_omits_limit_args_when_disabled(monkeypatch, capture):
    monkeypatch.setenv("QUEUE_MAX_LENGTH", "0")
    arguments = capture(monkeypatch, QUEUE_NAME)
    assert_dlx_pair(arguments, QUEUE_NAME)
    assert "x-max-length" not in arguments
    assert "x-overflow" not in arguments


def test_all_declaration_sites_emit_identical_tables(monkeypatch):
    monkeypatch.delenv("QUEUE_MAX_LENGTH", raising=False)
    tables = [capture(monkeypatch, QUEUE_NAME) for capture in DECLARATION_SITES]
    assert tables[0] == tables[1] == tables[2] == tables[3]
