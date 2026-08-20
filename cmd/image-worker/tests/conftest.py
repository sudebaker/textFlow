"""
Conftest for image-worker tests.

Sets sys.path so worker.py is imported from THIS worker directory, not from
another worker that pytest may have loaded first (all conftest files load
before any test module is imported).
"""

import sys
from pathlib import Path

import pytest

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
        captured = sys.modules.get("worker")
        if captured is not None and _worker_module is None:
            _worker_module = captured


def pytest_runtest_setup(item):
    """Before each test in THIS suite, restore sys.modules['worker'] to our module."""
    global _worker_module
    if Path(item.fspath).resolve().parent == _TESTS_DIR and _worker_module is not None:
        sys.modules["worker"] = _worker_module


@pytest.fixture(autouse=True)
def worker_runtime(monkeypatch, tmp_path):
    """Point the worker at a temp artifact store and /tmp uploads.

    worker.py reads STORE and UPLOAD_PATH from module globals (env-configured).
    Without this, process_message() writes to /app/data/artifacts and rejects
    files outside /app/data/uploads.
    """
    from pkg.worker_common.artifact_store import FSStore

    sys.modules.pop("worker", None)
    import worker as image_worker

    monkeypatch.setattr(image_worker, "UPLOAD_PATH", "/tmp")
    monkeypatch.setattr(image_worker, "STORE", FSStore(str(tmp_path)))
    return image_worker
