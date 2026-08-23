"""
Conftest for inference-worker tests.

Sets sys.path so worker.py is imported from THIS worker directory, not from
another worker that pytest may have loaded first (all conftest files load
before any test module is imported).
"""

import sys
from pathlib import Path

import pytest
from unittest.mock import Mock

# --- sys.path setup ---
WORKER_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = WORKER_DIR.parent.parent
_w = str(WORKER_DIR)
_r = str(PROJECT_ROOT)
for _p in (_w, _r):
    while _p in sys.path:
        sys.path.remove(_p)
sys.path.insert(0, _r)
sys.path.insert(0, _w)

# Evict stale 'worker' module so Python re-imports from the updated sys.path
sys.modules.pop("worker", None)

# ---------------------------------------------------------------------------
# pytest hooks to ensure sys.modules["worker"] always resolves to THIS
# worker's module when running tests from this directory.
# ---------------------------------------------------------------------------
_TESTS_DIR = Path(__file__).resolve().parent
_worker_module = None


def pytest_pycollect_makemodule(module_path, parent):
    """Fire before each test module is imported; set our worker's paths."""
    if Path(module_path).resolve().parent == _TESTS_DIR:
        for _p in (_w, _r):
            while _p in sys.path:
                sys.path.remove(_p)
        sys.path.insert(0, _r)
        sys.path.insert(0, _w)
        sys.modules.pop("worker", None)
    return None


def pytest_itemcollected(item):
    """After a test item is collected (module imported), capture the worker module."""
    global _worker_module
    if Path(item.fspath).resolve().parent == _TESTS_DIR:
        # The worker was renamed worker.py -> inference_worker.py. The tests
        # patch "worker.*" attributes, so alias the new module under the old
        # name to keep `unittest.mock.patch("worker.LLM_URL", ...)` working.
        captured = sys.modules.get("worker") or sys.modules.get("inference_worker")
        if captured is not None and _worker_module is None:
            sys.modules["worker"] = captured
            _worker_module = captured


def pytest_runtest_setup(item):
    """Before each test in THIS suite, restore sys.modules['worker'] to our module."""
    global _worker_module
    if Path(item.fspath).resolve().parent == _TESTS_DIR and _worker_module is not None:
        sys.modules["worker"] = _worker_module
        if "inference_worker" in sys.modules:
            sys.modules["inference_worker"] = _worker_module


@pytest.fixture(autouse=True)
def _reset_prometheus_registry():
    """Clear the Prometheus global registry before each test.

    Each InferenceWorker() registers BaseWorker metrics (jobs_total, etc.) on
    the process-wide default registry. Without a reset, instantiating more
    than one worker per test process raises
    'Duplicated timeseries in CollectorRegistry'.
    """
    from prometheus_client import REGISTRY

    collectors = list(REGISTRY._collector_to_names.keys())
    for c in collectors:
        REGISTRY.unregister(c)
    yield
    for c in list(REGISTRY._collector_to_names.keys()):
        REGISTRY.unregister(c)


@pytest.fixture(autouse=True)
def _mock_redis(monkeypatch):
    """Prevent tests from ever connecting to a real Redis.

    The worker fixture originally wrapped InferenceWorker() in a `with
    patch("redis.from_url")` whose context closed before the test body ran,
    so redis_client accessed inside tests attempted a real connection to
    localhost:6379 (with 30s of retry backoff). Patch it for the whole test.
    """
    import redis as redis_module

    class _NoopClient:
        def __init__(self, *a, **k):
            pass

        def ping(self):
            return True

        def __getattr__(self, name):
            return Mock()

    monkeypatch.setattr(redis_module, "from_url", lambda *a, **k: _NoopClient())


