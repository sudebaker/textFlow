"""
Conftest for inference-worker tests.

Mocks external dependencies (pika, redis, requests, prometheus_client) and
internal pkg modules (events_python, logging_python) so that BaseWorker and
InferenceWorker can be imported and instantiated without real connections.

IMPORTANT: Do NOT mock pkg.worker_common or pkg.worker_common.base — those
must remain real modules so InferenceWorker can inherit from BaseWorker.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

WORKER_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = WORKER_DIR.parent.parent

# Ensure paths are correct for imports
_w = str(WORKER_DIR)
_r = str(PROJECT_ROOT)
for _p in (_w, _r):
    while _p in sys.path:
        sys.path.remove(_p)
sys.path.insert(0, _r)
sys.path.insert(0, _w)

# Remove any cached 'worker' module so fresh import happens per test
sys.modules.pop("worker", None)

# --- Mock external libraries ---
# pika: fully mocked (no real connection needed)
sys.modules["pika"] = MagicMock()

# redis: fully mocked (no real connection needed)
sys.modules["redis"] = MagicMock()

# requests: fully mocked. Use lightweight stubs for the exception classes
# so the conftest remains hermetic and works even when requests is absent.
class _RequestExceptionStub(Exception):
    pass


class _TimeoutStub(_RequestExceptionStub):
    pass


_requests_mock = MagicMock()
_requests_mock.RequestException = _RequestExceptionStub
_requests_mock.Timeout = _TimeoutStub
sys.modules["requests"] = _requests_mock

# prometheus_client: fully mocked
sys.modules["prometheus_client"] = MagicMock()

# fastapi: fully mocked
sys.modules["fastapi"] = MagicMock()
sys.modules["fastapi.responses"] = MagicMock()

# --- Mock internal pkg modules that base.py imports ---
# pkg.events_python provides EventBus
sys.modules["pkg.events_python"] = MagicMock()
# pkg.logging_python provides setup_logging, JobLogger
sys.modules["pkg.logging_python"] = MagicMock()
