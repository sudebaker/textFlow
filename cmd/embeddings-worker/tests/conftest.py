"""
Conftest for embeddings-worker tests.

Sets sys.path so embeddings_worker.py and the app package are imported from
THIS worker directory, and pkg from the project root. Without this, pytest's
basedir insertion only exposes cmd/embeddings-worker (so pkg.* imports fail).
"""

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

# Evict stale modules so Python re-imports from the updated sys.path.
# 'app' is a generic package name shared by several workers; without the evict
# a cached app from another worker dir leaks into this suite.
sys.modules.pop("embeddings_worker", None)
sys.modules.pop("app", None)

# ---------------------------------------------------------------------------
# pytest hooks to keep the worker dir first on sys.path for this suite.
# ---------------------------------------------------------------------------
_TESTS_DIR = Path(__file__).resolve().parent


def pytest_pycollect_makemodule(module_path, parent):
    """Fire before each test module is imported; set our worker's paths."""
    if Path(module_path).resolve().parent == _TESTS_DIR:
        for _p in (_w, _r):
            while _p in sys.path:
                sys.path.remove(_p)
        sys.path.insert(0, _r)
        sys.path.insert(0, _w)
        sys.modules.pop("embeddings_worker", None)
        sys.modules.pop("app", None)
    return None
