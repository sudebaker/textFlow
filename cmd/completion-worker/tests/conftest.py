"""
Conftest for completion-worker tests.

Sets sys.path so worker.py is imported from THIS worker directory, not from
another worker that pytest may have loaded first (all conftest files load
before any test module is imported).
"""

import os
import sys
from pathlib import Path

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

# Point PipelineDefinition.load() at the repo config (Docker mounts it at
# /app/configs/pipeline.json).
os.environ.setdefault("PIPELINE_CONFIG_PATH", str(PROJECT_ROOT / "configs" / "pipeline.json"))

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
